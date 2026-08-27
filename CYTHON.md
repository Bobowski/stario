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
| Octet 64KB | 21,233 ± 146 | 21,289 ± 179 | 1.00× |
| Octet 2MB (buffer) | 2,111 ± 16 | 1,223 ± 14 | 0.58× |
| Octet 2MB (stream) | 2,885 ± 18 | 3,029 ± 18 | 1.05× |
| Multipart 2MB | 2,118 ± 41 | 1,670 ± 20 | 0.79× |

Read paths ~1.7×. Small POST ~1.2×. 64KB ingest even. Streaming 2MB slightly
ahead. Large buffered ingest and multipart still trail Python.
