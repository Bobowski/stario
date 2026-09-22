# Stario threads vs Granian workers (2026-09-21)

Same host, same wrk suite, same day as
[`baseline-threads-reuseport-20260921.md`](baseline-threads-reuseport-20260921.md).
Stario is `STARIO_THREADS=N` (`SO_REUSEPORT`, one process). Granian RSGI is
`--workers N --runtime-threads 1 --loop uvloop` (Granian 2.8.3, no framework).

`--workers` is the knob that adds event loops. `--runtime-threads` is Rust
Tokio I/O per worker and stays at 1 (Granian's own default; the suite has
always used that). On **3.14t**, Granian workers are **threads** in one
interpreter — the same scaling axis as `STARIO_THREADS`. On **GIL 3.14**,
Granian workers are **processes**. Both are measured.

Relative signal on one 4-vCPU host. wrk (`-t2`) and the server share the box.

## Reproduce

```bash
# 3.14t: in-process threads on both sides
for n in 1 2 4; do
  PYTHON_GIL=0 STARIO_THREADS=$n PYTHON=3.14t \
    DURATION=10s RUNS=5 WARMUP=1 THREADS=2 \
    CONNECTIONS=128 UPLOAD_CONNECTIONS=32 KEEP_RAW=1 \
    RESULT_NAME=threads-vs-granian-20260921/stario-$n \
    benchmarks/server/run.sh stario

  PYTHON_GIL=0 GRANIAN_WORKERS=$n PYTHON=3.14t \
    DURATION=10s RUNS=5 WARMUP=1 THREADS=2 \
    CONNECTIONS=128 UPLOAD_CONNECTIONS=32 KEEP_RAW=1 \
    RESULT_NAME=threads-vs-granian-20260921/granian-$n \
    benchmarks/server/run.sh granian-rsgi
done

# GIL 3.14: Granian process workers (production scale-out)
for n in 1 2 4; do
  GRANIAN_WORKERS=$n PYTHON=3.14 \
    DURATION=10s RUNS=5 WARMUP=1 THREADS=2 \
    CONNECTIONS=128 UPLOAD_CONNECTIONS=32 KEEP_RAW=1 \
    RESULT_NAME=threads-vs-granian-20260921/granian-gil-$n \
    benchmarks/server/run.sh granian-rsgi
done
```

Raw wrk under `benchmarks/server/results/threads-vs-granian-20260921/`
(gitignored). Stario / Granian 3.14t commit `f0f68f9`.

## Environment

| | |
|---|---|
| CPU | 4 vCPUs, Intel Xeon, 1 thread/core |
| RAM | 16 GiB |
| OS | Linux 6.12.94+ |
| Stario / Granian FT | 3.14.7 free-threading, `PYTHON_GIL=0`, uvloop |
| Granian processes | 3.14.7 GIL, uvloop |
| Client | wrk 4.1.0, `-t2 -c128` (GET / small JSON), `-c32` (64KB / 2MB) |
| Topology | wrk and server on the same host, `127.0.0.1` |
| Granian | 2.8.3 RSGI, `--runtime-threads 1` |

## Methodology

- **5 measured + 1 warmup**, `DURATION=10s`, IQR trimming via
  `benchmarks/server/stats.py`.
- Endpoints match `baseline-20260921.md`.
- No Non-2xx or socket timeouts in any wrk run.
- Stario N=1 here is a fresh pass (plaintext 120k ±1.8k), not the noisier
  106k cell in the reuseport write-up. Ratios below use this pass.
- Granian RSGI has **no router**: `request` is a `path.startswith` plus
  manual query/header reads. Stario still walks the trie + LRU.

## Median req/s

Bold is the highest cell in that row across the three servers at that N.

### N = 1

| Endpoint | Stario 3.14t | Granian 3.14t threads | Granian GIL processes |
| --- | ---: | ---: | ---: |
| Plaintext | 120,118 ± 1,829 | 127,252 ± 3,288 | **146,524 ± 298** |
| Request fields | 87,128 ± 1,229 | 120,307 ± 1,243 | **138,192 ± 2,216** |
| JSON small | 80,912 ± 729 | 103,397 ± 4,643 | **103,616 ± 1,848** |
| Octet 64KB | 32,947 ± 329 | **62,600 ± 1,287** | 30,933 ± 238 |
| Octet 2MB (buffer) | 2,021 ± 23 | **2,828 ± 78** | 1,441 ± 27 |
| Octet 2MB (stream) | 3,150 ± 71 | **4,951 ± 145** | 3,145 ± 201 |
| Multipart 2MB | 2,127 ± 62 | **2,711 ± 285** | 1,464 ± 4 |

### N = 2

| Endpoint | Stario 3.14t | Granian 3.14t threads | Granian GIL processes |
| --- | ---: | ---: | ---: |
| Plaintext | **228,060 ± 3,378** | 126,531 ± 14,288 | 196,015 ± 4,890 |
| Request fields | 140,175 ± 4,872 | 118,003 ± 8,809 | **163,934 ± 1,533** |
| JSON small | **171,027 ± 1,254** | 91,480 ± 1,481 | 137,357 ± 301 |
| Octet 64KB | **86,110 ± 424** | 48,472 ± 708 | 54,321 ± 854 |
| Octet 2MB (buffer) | **4,189 ± 58** | 4,138 ± 33 | 3,321 ± 8 |
| Octet 2MB (stream) | **5,773 ± 88** | 5,048 ± 38 | 4,121 ± 61 |
| Multipart 2MB | 3,388 ± 32 | **4,398 ± 476** | 3,304 ± 34 |

### N = 4

| Endpoint | Stario 3.14t | Granian 3.14t threads | Granian GIL processes |
| --- | ---: | ---: | ---: |
| Plaintext | **249,948 ± 1,374** | 155,479 ± 11,874 | 216,670 ± 4,308 |
| Request fields | 167,442 ± 1,455 | 164,587 ± 1,778 | **182,570 ± 625** |
| JSON small | **205,678 ± 570** | 131,356 ± 2,646 | 142,657 ± 5,265 |
| Octet 64KB | **105,972 ± 1,195** | 71,876 ± 1,443 | 66,075 ± 1,657 |
| Octet 2MB (buffer) | **5,358 ± 54** | 5,180 ± 27 | 3,940 ± 13 |
| Octet 2MB (stream) | **6,670 ± 51** | 5,481 ± 90 | 4,835 ± 70 |
| Multipart 2MB | 4,289 ± 28 | **5,200 ± 44** | 4,194 ± 146 |

## Scaling vs own N=1

| Endpoint | Stario 2 / 4 | Granian FT 2 / 4 | Granian GIL 2 / 4 |
| --- | ---: | ---: | ---: |
| Plaintext | 1.90× / 2.08× | 0.99× / 1.22× | 1.34× / 1.48× |
| Request fields | 1.61× / 1.92× | 0.98× / 1.37× | 1.19× / 1.32× |
| JSON small | 2.11× / 2.54× | 0.88× / 1.27× | 1.33× / 1.38× |
| Octet 64KB | 2.61× / 3.22× | 0.77× / 1.15× | 1.76× / 2.14× |
| Octet 2MB (buffer) | 2.07× / 2.65× | 1.46× / 1.83× | 2.30× / 2.73× |
| Octet 2MB (stream) | 1.83× / 2.12× | 1.02× / 1.11× | 1.31× / 1.54× |
| Multipart 2MB | 1.59× / 2.02× | 1.62× / 1.92× | 2.26× / 2.86× |

## Stario / Granian at the same N

| Endpoint | vs FT N=1 / 2 / 4 | vs GIL N=1 / 2 / 4 |
| --- | ---: | ---: |
| Plaintext | 0.94× / **1.80×** / **1.61×** | 0.82× / **1.16×** / **1.15×** |
| Request fields | 0.72× / **1.19×** / 1.02× | 0.63× / 0.85× / 0.92× |
| JSON small | 0.78× / **1.87×** / **1.57×** | 0.78× / **1.25×** / **1.44×** |
| Octet 64KB | 0.53× / **1.78×** / **1.47×** | **1.07×** / **1.59×** / **1.60×** |
| Octet 2MB (buffer) | 0.71× / 1.01× / 1.03× | **1.40×** / **1.26×** / **1.36×** |
| Octet 2MB (stream) | 0.64× / **1.14×** / **1.22×** | 1.00× / **1.40×** / **1.38×** |
| Multipart 2MB | 0.78× / 0.77× / 0.82× | **1.45×** / 1.03× / 1.02× |

## Read

**One loop.** GIL Granian is the N=1 GET winner (146k plaintext, 138k
request) — same band as `baseline-20260921.md`. Stario on 3.14t pays the
free-thread single-thread tax (120k / 87k). Granian 3.14t sits between
them on GET and is the N=1 upload winner (64KB **62.6k**, 2× GIL Granian
and ~2× Stario). Request stays Granian's: no router.

**Two loops / workers.** Stario's kernel `SO_REUSEPORT` does what we
wanted (~1.9–2.1× on plaintext/JSON). Granian 3.14t **thread** workers do
not: GET is flat-to-down vs its own N=1, with plaintext stdev ±14k.
Granian's production **process** workers do scale (plaintext **1.34×**,
64KB **1.76×**) but still land below Stario on plaintext / JSON / 64KB /
stream. Request is the leftover Granian win (164k vs Stario 140k).

**Four.** Same shape, smaller steps: this box is 4 vCPU plus wrk `-t2`.
Stario 250k plaintext vs GIL Granian 217k vs FT Granian 155k. JSON is
Stario **1.44×** GIL Granian. Request is still GIL Granian (183k vs 167k).
Multipart is the one case FT Granian keeps (5.2k vs Stario 4.3k).

So: against Granian's experimental 3.14t threads, Stario's N servers
scale and Granian's do not (yet). Against Granian's real scale-out
(GIL processes, no shared `Relay`), two Stario threads still beat two
Granian processes on the CPU cases that are not "hand-parsed request
line." Four of each is the same story, just closer to the 4-vCPU wall.
