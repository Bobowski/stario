# Stario benchmarks — averaged cloud run (20261013T102618Z)

**Method:** Each HTTP endpoint: **3** wrk runs (`-t2 -c128 -d10s`), report **mean ± stdev** (RPS).  
**Host:** 127.0.0.1 · **Python:** 3.14 · **Stario:** PyPI 5.0.0  
**VM:** 4 vCPU Intel Xeon KVM · 15 GiB RAM (shared Cursor Cloud Agent)

Full artifact dir: `benchmarks/server/results-cloud/averaged-20261013T102618Z/`  
Re-run: `REPS=3 DURATION=10s ./benchmarks/server/run-averaged-cloud.sh`

---

### Framework comparison (mean ± σ RPS)

| Server | Plaintext | JSON | Path param | JSON POST |
| --- | ---: | ---: | ---: | ---: |
| Stario 5.0.0 | 45,038 ± 382 | 42,566 ± 350 | 41,595 ± 567 | 39,503 ± 275 |
| BlackSheep 2.6 + Granian 2.8 | 102,821 ± 1,581 | 99,789 ± 3,212 | 88,893 ± 331 | 41,829 ± 450 |
| Robyn 0.88 | 183,423 ± 727 | 188,151 ± 15,177 | 30,722 ± 540 | 31,061 ± 435 |
| Sanic 25.12 | 46,498 ± 805 | 42,893 ± 780 | 42,247 ± 340 | 30,651 ± 392 |
| BlackSheep 2.6 + Uvicorn 0.54 | 50,200 ± 1,325 | 46,926 ± 1,715 | 45,421 ± 1,693 | 39,159 ± 1,079 |
| Falcon 4.4 + Uvicorn 0.54 | 43,369 ± 2,159 | 39,409 ± 2,291 | 40,004 ± 789 | 38,425 ± 734 |
| FastAPI 0.143 + Uvicorn 0.54 | 18,251 ± 263 | 17,718 ± 224 | 14,536 ± 159 | 12,360 ± 184 |

### Stario 4.3.0 vs 5.0.0 (mean of 3 wrk runs per cell)

| Endpoint | 4.3.0 | 5.0.0 | Δ |
| --- | ---: | ---: | ---: |
| GET /plaintext | 39,927 | 42,723 | 1.07× |
| GET /json | 38,196 | 41,676 | 1.09× |
| GET /user/42 | 34,392 | 40,558 | 1.18× |
| POST /validate | 31,897 | 37,039 | 1.16× |

*(Upgrade rows use separate cold starts per version; σ not shown — see `upgrade_samples.tsv`.)*

### HTML (50-row page, mean µs over 3× `compare.py`)

| Renderer | Mean µs |
| --- | ---: |
| Stario @baked | 41.5 |
| Jinja2 (compiled) | 107.5 |
| Stario naive | 265.2 |
| tdom (components) | 904.0 |
| tdom (naive t-string) | 920.4 |
| htpy | 943.1 |
| dominate | 1236.1 |

### Free-threaded `STARIO_THREADS` (3.14t, pin=1, wrk `-t1 -c128 -d15s`, mean ± σ)

| Threads | Plaintext | JSON |
| ---: | ---: | ---: |
| 1 | 121,890 ± 565 | 114,638 ± 1,751 |
| 2 | 192,992 ± 7,428 | 191,358 ± 2,414 |
| 4 | 157,666 ± 799 | 156,763 ± 1,554 |

**Note:** The framework suite (`wrk -t2`, CPython 3.14) and the threads suite (`wrk -t1`, 3.14t) ran in one pass; Stario absolute RPS differs between them because of client settings, interpreter, and VM load over the ~27-minute run. Use σ columns to judge stability; compare thread counts within the threads table, not directly to framework Stario rows.
