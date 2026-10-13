# Cloud VM benchmarks (fair setup, Oct 2026)

**VM:** 4 vCPU Intel Xeon (KVM) · 15 GiB · shared Cursor Cloud Agent  
**HTTP load:** `wrk -t2 -c128 -d15s` · **3–5 samples** → mean ± σ  
**Server rules:** one worker, access logs off, Stario compression off, `STARIO_TRACER=noop`, **uvloop** where supported.

## Fair head-to-head (interleaved, same pass)

Targets started in **round-robin** order (Stario → Stario → Granian …) so VM noise hits all equally.  
Artifact: `results-cloud/fair-20261013T110553Z/` · Re-run: `./benchmarks/server/run-fair-comparison.sh`

| Target | Plaintext | JSON | Path param | POST /validate |
| --- | ---: | ---: | ---: | ---: |
| **Stario 5.0.0** (CPython **3.14**, same as Granian) | 44,535 ± 569 | 42,590 ± 728 | 41,507 ± 464 | 39,360 ± 138 |
| **BlackSheep + Granian** (CPython 3.14) | **105,742 ± 1,832** | **98,785 ± 1,235** | **93,435 ± 2,116** | 41,527 ± 814 |
| Stario 5.0.0 (CPython **3.14t** only — not same ABI) | 241,381 ± 8,670 | 236,367 ± 8,145 | 236,967 ± 9,818 | 228,280 ± 5,841 |

On this **linux x86_64** host, the published **`cp314` Stario wheel is ~5× slower** than the **`cp314t` wheel** on the same routes and `wrk` settings (verified back-to-back). Granian and the rest of the suite use **3.14 (GIL)**; comparing Stario fairly to Granian means using **row 1**, where Stario is **~0.4× Granian** on GET rows — not 2×.

Blog / Apple Silicon numbers (~110k Stario vs ~120k Granian) are a different CPU and build; do not expect the cp314t row on Linux to match laptop 3.14 without checking the GIL wheel.

---

## Full framework table (CPython 3.14, 3× wrk from `averaged-20261013T102618Z`)

Non-Stario rows unchanged. **Stario row replaced** by fair interleaved **3.14** row above (5 samples, slightly longer `d15s`).

| Server | Plaintext | JSON | Path param | JSON POST |
| --- | ---: | ---: | ---: | ---: |
| **Stario 5.0.0 (3.14)** | **44,535 ± 569** | **42,590 ± 728** | **41,507 ± 464** | **39,360 ± 138** |
| BlackSheep + Granian | 102,821 ± 1,581 | 99,789 ± 3,212 | 88,893 ± 331 | 41,829 ± 450 |
| Robyn 0.88 | 183,423 ± 727 | 188,151 ± 15,177 | 30,722 ± 540 | 31,061 ± 435 |
| Sanic 25.12 | 46,498 ± 805 | 42,893 ± 780 | 42,247 ± 340 | 30,651 ± 392 |
| BlackSheep + Uvicorn | 50,200 ± 1,325 | 46,926 ± 1,715 | 45,421 ± 1,693 | 39,159 ± 1,079 |
| Falcon + Uvicorn | 43,369 ± 2,159 | 39,409 ± 2,291 | 40,004 ± 789 | 38,425 ± 734 |
| FastAPI + Uvicorn | 18,251 ± 263 | 17,718 ± 224 | 14,536 ± 159 | 12,360 ± 184 |

### Stario 4.3 vs 5.0 (both on CPython 3.14 in this VM)

| Endpoint | 4.3.0 | 5.0.0 (3.14) | Δ |
| --- | ---: | ---: | ---: |
| GET /plaintext | 39,927 | 44,535 | 1.12× |
| GET /json | 38,196 | 42,590 | 1.12× |
| GET /user/42 | 34,392 | 41,507 | 1.21× |
| POST /validate | 31,897 | 39,360 | 1.23× |

### HTML (mean µs, 3× `compare.py`)

| Renderer | Mean µs |
| --- | ---: |
| Stario @baked | 41.5 |
| Jinja2 | 107.5 |
| Stario naive | 265.2 |

---

**Scripts:** `./benchmarks/server/run-fair-comparison.sh` (Stario 3.14 / 3.14t / Granian interleaved) · `./benchmarks/server/run-averaged-cloud.sh` (full matrix, all **3.14**).
