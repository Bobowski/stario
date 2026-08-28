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

## Hot path vs `cython-core` (2026-08-28)

Same machine, official suite, unmodified `cython-core` (`f80d6f0`, run
`20260828T110114Z`) vs this hot-path work (`174cef7`, run
`20260828T112246Z`). Full table: `benchmarks/server/baseline-20260828.md`.
Do not mix these Cython rows with the 2026-08-27 Python column (different
host class).

| Endpoint | Unmodified `cython-core` | Hot path | Hot / core |
| --- | ---: | ---: | ---: |
| Plaintext | 129,230 ± 2,877 | 132,083 ± 4,307 | 1.02× |
| JSON | 123,825 ± 3,502 | 131,544 ± 825 | 1.06× |
| Params | 111,894 ± 1,687 | 126,443 ± 252 | 1.13× |
| Validate JSON | 66,359 ± 234 | 108,084 ± 1,902 | **1.63×** |
| Form POST | 77,785 ± 1,875 | 131,462 ± 1,629 | **1.69×** |
| JSON 1KB | 67,892 ± 1,636 | 112,493 ± 3,605 | **1.66×** |
| Octet 64KB | 24,091 ± 164 | 37,735 ± 131 | **1.57×** |
| Octet 2MB (buffer) | 3,117 ± 78 | 3,190 ± 31 | 1.02× |
| Octet 2MB (stream) | 3,480 ± 3 | 3,485 ± 12 | 1.00× |
| Multipart 2MB | 3,179 ± 151 | 3,350 ± 37 | 1.05× |

Small POST and the 64KiB octet fixture (exactly the deferral cap) jump because
`body()` no longer waits on an Event during `llhttp_execute`. 2MB paths stay
header-dispatch and did not regress. Plaintext is within sample noise.
