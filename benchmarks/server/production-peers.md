# Stario vs aiohttp / Litestar / Pyronova (this machine)

Same 10 honest routes as `apps/stario_app.py`. No cached JSON, no
`add_fast_response`, no `drain_count()`. Fill-the-box uses each
stack’s production scale knob. 1-worker is the code-path row.

| | |
|---|---|
| Host | Intel Xeon (KVM), 4 vCPUs, 15 GiB |
| Client | wrk 4.1.0, `-t2 -c128` (read/small upload), `-c32` (64KB/2MB) |
| Samples | 10s × 3 measured + 1 warmup, IQR trim |
| Python | 3.14.7 (uv) |
| Fill-the-box raw | `results/20260825T200246Z` |
| 1-worker raw | `results/20260825T203223Z` |

Versions: aiohttp 3.14.3 + uvloop; Litestar 2.24.0 + uvicorn 0.52.4
(uvloop/httptools, msgspec); Pyronova 2.7.0; Stario Cython this
checkout.

Reproduce:

```bash
# fill the box
BENCH_PROCS=$(nproc) benchmarks/server/run.sh stario-cython-n aiohttp-n litestar-n pyronova-n

# code path
benchmarks/server/run.sh stario-cython aiohttp litestar pyronova
```

## 1 worker (code path)

| Server | Plaintext | JSON | Params | Validate |
| --- | ---: | ---: | ---: | ---: |
| Pyronova (1 TPC thread) | **143,243 ± 6,386** | **141,818 ± 2,400** | **134,950 ± 4,177** | 86,873 ± 2,671 |
| **Stario Cython** | 129,297 ± 5,686 | 113,894 ± 7,383 | 113,931 ± 2,252 | **105,918 ± 4,901** |
| aiohttp + uvloop | 51,264 ± 74 | 49,950 ± 241 | 44,676 ± 140 | 34,887 ± 407 |
| Litestar + Uvicorn | 30,353 ± 73 | 29,227 ± 546 | 25,423 ± 259 | 19,796 ± 81 |

vs Cython plaintext: Pyronova **1.11×**, aiohttp **0.40×**, Litestar **0.23×**.

| Server | Form | JSON 1K | 64KB | 2MB buf | 2MB stream | Multipart |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Pyronova | **125,281** | **126,310** | **50,074** | 1,455 | **4,756** | 1,501 |
| **Stario Cython** | 122,385 | 101,376 | 34,396 | **2,598** | 2,650 | **2,671** |
| aiohttp | 41,867 | 36,300 | 15,606 | 1,316 | 2,412 | 1,273 |
| Litestar | 23,542 | 19,936 | 13,995 | 1,303 | 1,171 | 1,303 |

## Fill the box (4 units)

| Server | How |
| --- | --- |
| Stario Cython | 4 processes, `STARIO_REUSE_PORT=1` |
| aiohttp | 4 processes, `SO_REUSEPORT`, uvloop |
| Litestar | uvicorn `--workers 4`, uvloop + httptools |
| Pyronova | `PYRONOVA_WORKERS=4` (TPC) — **aborted** |

| Server | Plaintext | JSON | Params | Validate |
| --- | ---: | ---: | ---: | ---: |
| **Stario Cython ×4** | **282,133 ± 11,925** | **282,335 ± 21,273** | **276,876 ± 2,890** | **244,755 ± 4,140** |
| aiohttp ×4 | 155,411 ± 555 | 149,412 ± 580 | 138,522 ± 1,993 | 116,685 ± 412 |
| Litestar ×4 | 100,635 ± 5,848 | 98,303 ± 5,814 | 77,320 ± 8,746 | 71,323 ± 889 |
| Pyronova ×4 | crash | crash | crash | crash |

vs Cython plaintext: aiohttp **0.55×**, Litestar **0.36×**.

| Server | Form | JSON 1K | 64KB | 2MB buf | 2MB stream | Multipart |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| **Stario Cython ×4** | **287,042** | **258,560** | **115,650** | **6,323** | **6,289** | **6,406** |
| aiohttp ×4 | 128,922 | 117,868 | 63,592 | 3,974 | 5,808 | 3,794 |
| Litestar ×4 | 79,886 | 56,280 | 47,111 | 3,108 | 4,470 | 3,259 |
| Pyronova ×4 | crash | | | | | |

Scaling plaintext 1 → 4: Cython 129k → 282k (**2.18×**), aiohttp 51k → 155k
(**3.03×**), Litestar 30k → 101k (**3.31×**). aiohttp/Litestar scale
closer to linear; Cython still finishes far ahead.

Pyronova 2.7.0 on CPython 3.14 here: `TPC threads >= 2` dies under wrk
with `double free or corruption` / `munmap_chunk(): invalid pointer` /
SIGSEGV. One TPC thread is stable. Engine bug, not our handler.
HttpArena’s 826k is 64 workers on their box.

## Read against HttpArena

HttpArena (64-core, gcannon, Alpha Round): aiohttp composite **240**
(#1 flagship Python), Litestar **236**, Pyronova experimental/tuned
**431**. Those RPS figures are not this box.

On **this** 4-vCPU box, honest handlers:

- **Litestar** is Class B and the slowest of the three Python
  frameworks we just ran. ~4.3× behind 1-worker Cython, ~2.8× behind
  ×4. HttpArena closeness to aiohttp is completeness + their iron,
  not a speed tie on our routes.
- **aiohttp** is the right Class B opponent. We are **~2.5×** (1
  worker) and **~1.8×** (×4) on plaintext; validate is the same ratio.
- **Pyronova** is Class A (Rust Hyper). 1-worker they take hello-world
  / JSON / stream (**1.11×** plaintext); we take validate and buffered
  2MB. They cannot fill this box (crash at 2+ TPC threads).

Do not say “faster than HttpArena aiohttp.” Say “faster than
production aiohttp and Litestar on this machine, same routes.”
Do not say “faster than Pyronova” without the 1-worker split.
