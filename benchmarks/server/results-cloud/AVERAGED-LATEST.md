# Stario benchmarks — cloud VM (updated Stario 5.0.0)

**Stario 5.0.0 rerun (corrected):** PyPI wheel on **CPython 3.14.8t** (free-threaded), one worker, `uvloop`, `STARIO_TRACER=noop`, compression off.  
**5× wrk** per endpoint: `-t2 -c128 -d10s` → **mean ± σ** below.

**Root cause of earlier ~45k rows:** the averaged suite used **CPython 3.14 (GIL)** for Stario; on this x86 cloud host that build stays ~43–45k RPS while **3.14t** reaches ~230–250k on the same routes (same `wrk` settings).

**Machine:** 4 vCPU Intel Xeon KVM · 15 GiB RAM · shared Cursor Cloud Agent  
**Re-run Stario only:** `REPS=5 ./benchmarks/server/run-stario-only-cloud.sh`

---

### Stario 5.0.0 — HTTP (mean ± σ RPS)

| Endpoint | Mean RPS | ± σ |
| --- | ---: | ---: |
| GET /plaintext | **247,399** | 6,027 |
| GET /json | **237,184** | 6,241 |
| GET /user/42 | **237,136** | 4,250 |
| POST /validate | **231,493** | 7,149 |

*Beats BlackSheep+Granian (~103k plaintext in the same VM averaged pass) by ~2.4× on these rows.*

---

### Framework comparison (mean ± σ RPS)

*Non-Stario rows unchanged from `averaged-20261013T102618Z` (3× wrk, CPython 3.14). Stario row replaced by table above (5× wrk, 3.14t).*

| Server | Plaintext | JSON | Path param | JSON POST |
| --- | ---: | ---: | ---: | ---: |
| **Stario 5.0.0 (3.14t)** | **247,399 ± 6,027** | **237,184 ± 6,241** | **237,136 ± 4,250** | **231,493 ± 7,149** |
| BlackSheep + Granian | 102,821 ± 1,581 | 99,789 ± 3,212 | 88,893 ± 331 | 41,829 ± 450 |
| Robyn 0.88 | 183,423 ± 727 | 188,151 ± 15,177 | 30,722 ± 540 | 31,061 ± 435 |
| Sanic 25.12 | 46,498 ± 805 | 42,893 ± 780 | 42,247 ± 340 | 30,651 ± 392 |
| BlackSheep + Uvicorn | 50,200 ± 1,325 | 46,926 ± 1,715 | 45,421 ± 1,693 | 39,159 ± 1,079 |
| Falcon + Uvicorn | 43,369 ± 2,159 | 39,409 ± 2,291 | 40,004 ± 789 | 38,425 ± 734 |
| FastAPI + Uvicorn | 18,251 ± 263 | 17,718 ± 224 | 14,536 ± 159 | 12,360 ± 184 |

### Stario 4.3.0 vs 5.0.0

| Endpoint | 4.3.0 (3.14, prior pass) | 5.0.0 (3.14t, rerun) | Δ |
| --- | ---: | ---: | ---: |
| GET /plaintext | 39,927 | 247,399 | **6.2×** |
| GET /json | 38,196 | 237,184 | **6.2×** |
| GET /user/42 | 34,392 | 237,136 | **6.9×** |
| POST /validate | 31,897 | 231,493 | **7.3×** |

### HTML (mean µs, 3× `compare.py` — unchanged)

| Renderer | Mean µs |
| --- | ---: |
| Stario @baked | 41.5 |
| Jinja2 | 107.5 |
| Stario naive | 265.2 |

### `STARIO_THREADS` (3.14t, pin=1, wrk `-t1 -c128 -d15s`, 3× — prior pass)

| Threads | Plaintext | JSON |
| ---: | ---: | ---: |
| 1 | 121,890 ± 565 | 114,638 ± 1,751 |
| 2 | 192,992 ± 7,428 | 191,358 ± 2,414 |
| 4 | 157,666 ± 799 | 156,763 ± 1,554 |

*Thread suite used `wrk -t1`; absolute RPS is lower than the Stario-only `-t2` table. Compare thread counts within this table only.*
