# Stario `STARIO_THREADS` 1 vs 2 vs 4 (2026-09-21)

Same checkout, same machine, same wrk settings. Only the server worker
count changes.

Relative signal on one 4-vCPU host — not lab-grade absolutes. wrk and the
server share the box, so 4 worker loops plus `wrk -t2` oversubscribe the
cores. Treat 4-thread plaintext as noisy.

## Reproduce

```bash
# 3.14t: python-brotli is not marked free-threaded and would turn the GIL
# back on. PYTHON_GIL=0 keeps it off. Bench compression is disabled; the
# native protocol talks to libbrotli directly.
uv python install cpython-3.14.7+freethreaded

for n in 1 2 4; do
  PYTHON_GIL=0 STARIO_THREADS=$n \
    PYTHON=3.14t DURATION=10s RUNS=5 WARMUP=1 \
    THREADS=2 CONNECTIONS=128 UPLOAD_CONNECTIONS=32 \
    benchmarks/server/run.sh stario
done
```

Interpreter: CPython **3.14.7 free-threading** (`python3.14t`), `STARIO_LOOP=uvloop`.

## Environment

| | |
|---|---|
| CPU | 4 vCPUs, Intel Xeon, 1 thread/core |
| RAM | 16 GiB |
| OS | Linux 6.12.94+ |
| Python | 3.14.7 free-threading (`PYTHON_GIL=0`) |
| Client | wrk 4.1.0, `-t2 -c128` (GET / small JSON), `-c32` (64KB / 2MB) |
| Topology | wrk and server on the same host, `127.0.0.1` |
| Commit | `c114fc5` (`cursor/free-threading-research-0a7b`) |

## Methodology

- One process. `STARIO_THREADS=N` is N asyncio/uvloop loops, one per OS thread.
- Endpoints: `plaintext`, `request`, `json-small`, `post-octet-64k`,
  `post-octet-2m`, `post-stream-2m`, `multipart-2m` (same suite as
  `baseline-20260921.md`).
- **5 measured + 1 warmup**, `DURATION=10s`, IQR trimming via
  `benchmarks/server/stats.py`.
- No Non-2xx or socket timeouts in any wrk run.

N=1 on 3.14t is slower than the GIL 3.14 plaintext band in
`baseline-20260921.md` (~148k). That single-thread tax is expected; the
bet is N loops.

## Median req/s

Bold is the highest cell in that row. Ratio is vs `STARIO_THREADS=1`.

### GET

| Endpoint | 1 thread | 2 threads | 4 threads | 2 / 1 | 4 / 1 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Plaintext | 129,188 ± 4,033 | 229,989 ± 4,040 | **266,758 ± 65,551** | 1.78× | 2.06× |
| Request fields | 94,006 ± 2,482 | 127,337 ± 22,889 | **171,583 ± 12,925** | 1.35× | 1.83× |

### Upload / async

| Endpoint | 1 thread | 2 threads | 4 threads | 2 / 1 | 4 / 1 |
| --- | ---: | ---: | ---: | ---: | ---: |
| JSON small | 85,655 ± 2,630 | 168,802 ± 3,329 | **217,680 ± 2,746** | 1.97× | 2.54× |
| Octet 64KB | 34,853 ± 104 | 84,127 ± 4,005 | **108,386 ± 4,130** | 2.41× | 3.11× |
| Octet 2MB (buffer) | 2,251 ± 16 | 4,306 ± 136 | **5,553 ± 93** | 1.91× | 2.47× |
| Octet 2MB (stream) | 3,469 ± 170 | 5,468 ± 280 | **6,826 ± 109** | 1.58× | 1.97× |
| Multipart 2MB | 2,382 ± 7 | 4,444 ± 43 | **5,676 ± 51** | 1.87× | 2.38× |

## Read

Two loops is the clean win: plaintext **1.78×**, JSON **1.97×**, 64KB
**2.41×**. Request-fields scaling is weaker and noisier (some 2-thread
samples sit at the 1-thread rate).

Four loops still go up, but this 4-vCPU box is sharing cores with wrk.
Plaintext stdev blows out (one measured sample at 123k next to 266–274k).
JSON and 64KB stay tight and keep climbing (**2.54×** / **3.11×** vs one
loop). Large uploads are in the same band: about 2–2.5× at four loops.

Do not read 4-thread plaintext as a 4× CPU win. Read JSON / 64KB as the
cases where extra loops actually buy handler CPU.
