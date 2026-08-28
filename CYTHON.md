# Cython protocol (cython-core)

This worktree is `projects/stario` on branch `cython-core`. uvloop owns the
socket and llhttp parses in C. The protocol lives in `src/stario_cython`.

`projects/stario` stays on `main` and stays a workspace member. Do not
`uv sync` this folder from the monorepo root. Use an isolated venv:

```bash
cd projects/stario-cython
# macOS
brew install pkg-config brotli
uv venv --python 3.14
uv pip install --python .venv/bin/python -e ".[uvloop]" cython setuptools wheel pytest pytest-asyncio
.venv/bin/python setup.py
PYTHONPATH=src:. .venv/bin/python -m stario_cython examples.cython.hello:bootstrap
```

Linux builds need `pkg-config` and the Brotli development package. Gzip
links system zlib (`-lz`). The native protocol offers `br` and `gzip`
only. Python Stario still negotiates zstd.

`STARIO_HOST` and `STARIO_PORT` set the bind address.

`PYTHONPATH=src` is required so `stario_cython` resolves after the inplace
build.

## Python vs Cython (2026-08-27)

Official `benchmarks/server` suite, same machine, one worker, `10s` × 5 measured
+ 1 warmup, IQR trimming. Full tables and reproduce steps:
`benchmarks/server/baseline-20260827.md`.

| Endpoint | Python httptools | Cython llhttp | Cython / Python |
| --- | ---: | ---: | ---: |
| Plaintext | 72,734 ± 647 | 125,160 ± 298 | 1.72× |
| JSON | 71,042 ± 582 | 123,017 ± 3,211 | 1.73× |
| Params | 71,033 ± 1,669 | 117,136 ± 2,590 | 1.65× |
| Validate JSON | 56,663 ± 1,621 | 69,992 ± 838 | 1.24× |
| Form POST | 60,476 ± 1,105 | 73,163 ± 361 | 1.21× |
| JSON 1KB | 56,451 ± 276 | 66,933 ± 104 | 1.19× |
| Octet 64KB | 25,228 ± 219 | 24,229 ± 244 | 0.96× |
| Octet 2MB (buffer) | 2,062 ± 292 | 2,947 ± 35 | 1.43× |
| Octet 2MB (stream) | 2,863 ± 15 | 3,208 ± 8 | 1.12× |
| Multipart 2MB | 2,047 ± 173 | 2,905 ± 60 | 1.42× |

Read paths ~1.7×. Small POST ~1.2×. 64KB ingest even. Buffered 2MB, streaming 2MB,
and multipart are ahead of Python after pre-sizing Content-Length bodies and
pausing `body()` at 64KiB between parser quantums.

## Hot path + timeouts vs `cython-core` (2026-08-28)

Same machine, official suite, unmodified `cython-core` (`f80d6f0`, run
`20260828T153450Z`) vs this branch (`460114d`, run `20260828T154518Z`).
Full table: `benchmarks/server/baseline-20260828.md`.
Do not mix these Cython rows with the 2026-08-27 Python column (different
host class), or with the earlier same-day PR #33-only capture
(`20260828T110114Z`) from a prior VM.

| Endpoint | Unmodified `cython-core` | This branch | Ratio |
| --- | ---: | ---: | ---: |
| Plaintext | 125,495 ± 879 | 130,032 ± 602 | **1.04×** |
| JSON | 121,997 ± 2,671 | 119,338 ± 4,601 | 0.98× |
| Params | 117,005 ± 211 | 122,350 ± 1,991 | **1.05×** |
| Validate JSON | 67,425 ± 512 | 106,918 ± 1,108 | **1.59×** |
| Form POST | 75,089 ± 1,124 | 126,328 ± 3,486 | **1.68×** |
| JSON 1KB | 68,866 ± 93 | 108,776 ± 781 | **1.58×** |
| Octet 64KB | 22,013 ± 390 | 37,948 ± 406 | **1.72×** |
| Octet 2MB (buffer) | 3,004 ± 14 | 3,172 ± 71 | **1.06×** |
| Octet 2MB (stream) | 3,435 ± 48 | 3,391 ± 120 | 0.99× |
| Multipart 2MB | 2,998 ± 59 | 3,178 ± 18 | **1.06×** |

Small POST and the 64KiB octet fixture (exactly the deferral cap) jump because
`body()` no longer waits on an Event during `llhttp_execute`. Header/idle
timeouts reuse one timer handle per connection; plaintext is **+3.6%** (not
killed). JSON and 2MB stream sit inside sample noise. 2MB buffer did not
regress.

Header, idle, and body-stall timeouts share one connection-set sweeper
(`loop.time()` once per wake, default 50ms). Idle is armed only when the
connection is idle; trickle bytes do not reset the header deadline.
`RequestPolicy.max_pipelined_requests` (default 8) caps the pipeline queue.
Body stall is a generation counter — chunks do not `call_later`.

Same-machine callback vs 10ms-sweep vs timeouts-off:
`benchmarks/server/baseline-20260828.md`. Sweep ≈ callback on plaintext;
timeouts-off is ~+5% plaintext (not worth dropping timeouts);
10ms sweep was −7% on 2MB stream, which is why the period is 50ms.
