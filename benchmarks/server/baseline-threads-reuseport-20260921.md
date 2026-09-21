# Stario `STARIO_THREADS` 1 vs 2 vs 4 — `SO_REUSEPORT` (2026-09-21)

Same checkout, same machine, same wrk settings as
[`baseline-threads-20260921.md`](baseline-threads-20260921.md). That earlier
capture used one acceptor plus `connect_accepted_socket` handoff. This one is
**N full servers**: thread 0 bootstraps, installs signals, fans out shutdown,
**and** listens. Threads `1..N-1` are the same kind of server. Each binds the
TCP port with `SO_REUSEPORT` (`create_server(..., reuse_port=True)`); the
kernel load-balances new connections. There is no per-accept
`call_soon_threadsafe`. If the kernel cannot share the listen address, Stario
stays at one thread.

Relative signal on one 4-vCPU host — not lab-grade absolutes. wrk and the
server share the box, so 4 worker loops plus `wrk -t2` oversubscribe the
cores.

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

Raw wrk under `benchmarks/server/results/threads-reuseport-20260921/`
(gitignored). Commit of the server: `23861ec`.

## Environment

| | |
|---|---|
| CPU | 4 vCPUs, Intel Xeon, 1 thread/core |
| RAM | 16 GiB |
| OS | Linux 6.12.94+ |
| Python | 3.14.7 free-threading (`PYTHON_GIL=0`) |
| Client | wrk 4.1.0, `-t2 -c128` (GET / small JSON), `-c32` (64KB / 2MB) |
| Topology | wrk and server on the same host, `127.0.0.1` |
| Commit | `23861ec` (`cursor/free-threading-research-0a7b`) |
| Listen | `N=1` historical `create_server`; `N>1` `SO_REUSEPORT` |

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
bet is N loops. N=1 does **not** take the reuseport path.

## Median req/s

Bold is the highest cell in that row. Ratio is vs `STARIO_THREADS=1`.

### GET

| Endpoint | 1 thread | 2 threads | 4 threads | 2 / 1 | 4 / 1 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Plaintext | 105,740 ± 17,959 | 226,181 ± 2,748 | **262,142 ± 4,590** | 2.14× | 2.48× |
| Request fields | 94,445 ± 2,392 | 140,104 ± 1,558 | **169,693 ± 2,876** | 1.48× | 1.80× |

### Upload / async

| Endpoint | 1 thread | 2 threads | 4 threads | 2 / 1 | 4 / 1 |
| --- | ---: | ---: | ---: | ---: | ---: |
| JSON small | 71,149 ± 3,877 | 163,203 ± 376 | **208,738 ± 3,485** | 2.29× | 2.93× |
| Octet 64KB | 34,302 ± 341 | 84,528 ± 5,957 | **103,752 ± 822** | 2.46× | 3.02× |
| Octet 2MB (buffer) | 2,048 ± 231 | 4,242 ± 18 | **4,968 ± 240** | 2.07× | 2.43× |
| Octet 2MB (stream) | 3,319 ± 126 | **6,490 ± 52** | 5,858 ± 240 | 1.96× | 1.76× |
| Multipart 2MB | 2,154 ± 268 | 4,337 ± 46 | **5,203 ± 79** | 2.01× | 2.42× |

## Read

Two loops is the clean win, and on this host it is closer to **2×** than the
handoff capture was: plaintext **2.14×**, JSON **2.29×**, 64KB **2.46×**.
Request-fields still scale less (**1.48×**) because that case is more
application work per request, but the 2-thread samples are tight
(±1.6k vs ±23k on handoff).

Four loops still go up on CPU-bound cases (JSON **2.93×**, 64KB **3.02×**),
and 4-thread plaintext is **stable** this time (262k ±4.6k, not ±65k).
This 4-vCPU box is sharing cores with wrk: 4 worker loops + `wrk -t2` is
six runnable threads. Do not read 4-thread plaintext as a 4× CPU win.

The 2MB stream case is the exception: **4 threads is slower than 2**
(5,858 vs 6,490). Large streaming uploads already saturate the box at two
loops; extra listeners just fight wrk for the same four cores.

## Versus handoff (`c114fc5`)

Medians at N=2 and N=4 land in the same band. The reuseport change is not
a throughput jump on this host — it is a simpler runtime (N threads, not
N+1; kernel accept; no per-connection hop) with **tighter** 2/4-thread
GET samples.

| Endpoint | Handoff 2 / 4 | Reuseport 2 / 4 |
| --- | ---: | ---: |
| Plaintext | 229,989 ± 4,040 / 266,758 ± **65,551** | 226,181 ± 2,748 / 262,142 ± **4,590** |
| Request fields | 127,337 ± **22,889** / 171,583 | 140,104 ± **1,558** / 169,693 |
| JSON small | 168,802 / 217,680 | 163,203 / 208,738 |
| Octet 64KB | 84,127 / 108,386 | 84,528 / 103,752 |

N=1 this run is noisier and lower (plaintext 106k ±18k vs 129k ±4k). That
path does not use `SO_REUSEPORT`; treat it as run-to-run jitter, not a
reuseport regression. Ratios vs this N=1 therefore look a bit more
optimistic than the handoff table.

## Takeaway

Kernel `SO_REUSEPORT` is enough on Linux TCP. Two threads pack this box.
Four threads keep climbing on JSON / 64KB and stop helping (or hurt) once
the workload is already I/O-and-core bound next to wrk. Unix sockets on
this kernel cannot dual-bind (`EOPNOTSUPP`); those stay at one thread.
