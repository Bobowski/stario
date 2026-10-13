# Stario benchmarks — averaged cloud run (20261013T102618Z)

**Method:** Each HTTP endpoint: **3** wrk runs (`-t2 -c128 -d10s`), report **mean ± stdev** (RPS).  
**Host:** 127.0.0.1 · **Python:** 3.14 · **Stario:** PyPI 5.0.0 (framework suite)  
**Note:** Shared 4-vCPU cloud VM; averages dampen run-to-run noise but not neighbor load.

### System
See `../system-info.txt` and `run.log`.

### Framework comparison (mean ± σ RPS)

| Target | Plaintext | JSON | Params | Validate |
| --- | ---: | ---: | ---: | ---: |
| stario | 45038 ± 382 | 42566 ± 350 | 41595 ± 567 | 39503 ± 275 |
| fastapi | 18251 ± 263 | 17718 ± 224 | 14536 ± 159 | 12360 ± 184 |
| blacksheep-uvicorn | 50200 ± 1325 | 46926 ± 1715 | 45421 ± 1693 | 39159 ± 1079 |
| blacksheep-granian | 102821 ± 1581 | 99789 ± 3212 | 88893 ± 331 | 41829 ± 450 |
| sanic | 46498 ± 805 | 42893 ± 780 | 42247 ± 340 | 30651 ± 392 |
| falcon | 43369 ± 2159 | 39409 ± 2291 | 40004 ± 789 | 38425 ± 734 |
| robyn | 183423 ± 727 | 188151 ± 15177 | 30722 ± 540 | 31061 ± 435 |

### Stario 4.3.0 vs 5.0.0 (mean RPS per endpoint)

| Endpoint | Stario 4.3.0 | Stario 5.0.0 |
| --- | ---: | ---: |
| /plaintext | 39927 | 42723 |
| /json | 38196 | 41676 |
| /user/42 | 34392 | 40558 |
| /validate | 31897 | 37039 |

### HTML render (mean µs over 3 runs of compare.py)

| Renderer | Mean µs (n=3 runs) |
| --- | ---: |
| stario @baked | 41.5 |
| jinja2 (compiled, autoescape) | 107.5 |
| stario naive (tree per call) | 265.2 |
| tdom (components) | 904.0 |
| tdom (naive t-string) | 920.4 |
| htpy | 943.1 |
| dominate | 1236.1 |

### Free-threaded `STARIO_THREADS` (3.14t, `STARIO_PIN_THREADS=1`, wrk `-t1 -c128 -d15s`)

| Threads | Plaintext (mean ± σ) | JSON (mean ± σ) |
| ---: | ---: | ---: |
| 1 | 121890 ± 565 | 114638 ± 1751 |
| 2 | 192992 ± 7428 | 191358 ± 2414 |
| 4 | 157666 ± 799 | 156763 ± 1554 |

Raw TSV: `framework_samples.tsv`, `upgrade_samples.tsv`, `threads_samples.tsv`.
