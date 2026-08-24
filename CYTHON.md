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
