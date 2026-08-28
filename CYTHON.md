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

## Merge snapshot (2026-08-28)

Official `benchmarks/server` suite, one worker, `10s` × 5 measured + 1
warmup, IQR trimming. Full lab log:
[`benchmarks/server/baseline-20260828.md`](benchmarks/server/baseline-20260828.md).

### vs unmodified `cython-core` (current Cython)

`f80d6f0` (`20260828T153450Z`) → this branch `460114d`
(`20260828T154518Z`). Same host, sequential.

| Endpoint | Current (`cython-core`) | This branch | Ratio |
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

Small POST and the 64KiB fixture jump because deferred Content-Length ≤64KiB
bodies complete before the handler’s `body()` wait. GET / 2MB stream are
noise. Timeouts did not kill plaintext (+3.6%).

### vs Python httptools (current Python Stario)

Same suite `20260828T193136Z`. Python: `stario.cli serve`. Cython:
`python -m stario_cython`.

| Endpoint | Current Python | This Cython | Cython / Python |
| --- | ---: | ---: | ---: |
| Plaintext | 74,752 ± 274 | 132,903 ± 3,484 | **1.78×** |
| JSON | 72,983 ± 1,742 | 133,817 ± 7,033 | **1.83×** |
| Params | 70,804 ± 95 | 126,707 ± 5,128 | **1.79×** |
| Validate JSON | 56,566 ± 619 | 112,068 ± 4,145 | **1.98×** |
| Form POST | 60,305 ± 106 | 123,813 ± 1,677 | **2.05×** |
| JSON 1KB | 55,945 ± 431 | 110,946 ± 4,462 | **1.98×** |
| Octet 64KB | 20,065 ± 202 | 37,772 ± 487 | **1.88×** |
| Octet 2MB (buffer) | 1,981 ± 33 | 3,441 ± 20 | **1.74×** |
| Octet 2MB (stream) | 2,969 ± 7 | 3,652 ± 16 | **1.23×** |
| Multipart 2MB | 2,007 ± 80 | 3,563 ± 99 | **1.77×** |

GET ~1.8×. Small POST ~2.0× (was ~1.2× on 27 Aug). 2MB stream 1.23×.

### Timeouts that land

Header, idle, and body-stall share **one** cleanup: the Date-header tick
(1s) calls `check_timeouts(now)` on live connections. One `loop.time()`
per wake. No extra sweeper, no per-request `TimerHandle`, no stall
`call_later` per chunk.

| | |
| --- | --- |
| Header | 5s, while headers or a deferred small body are still arriving |
| Idle | 5s, only when the connection is idle |
| Body stall | 30s, generation counter on the exchange |
| Pipeline cap | 8 |
| Tests | fallback sweeper at 50ms (`STARIO_CYTHON_TIMEOUT_SWEEP`) |
| Hatch | `STARIO_CYTHON_TIMEOUTS=off` |

wrk: sweep ≈ callbacks on plaintext; timeouts-off ~+5% plaintext (keep
timeouts); 10ms sweep −7% on 2MB stream; 50ms and 1s Date tick are a wash
(129.3k / 130.8k / 128.0k). Callbacks were deleted.

App/Router stay Python. Do not revive dual Cython App/Router, a contiguous
serializer, pooled `asyncio.Event`, or static/pre-serialized handlers.
