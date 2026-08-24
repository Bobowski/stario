# Stario benchmarks

Two suites, one per layer we care about:

- `html/` — HTML generation speed: stario against other Python renderers, plus
  microbenchmarks for stario's own hot paths.
- `server/` — end-to-end HTTP throughput: Stario against native Python HTTP
  servers and ASGI framework stacks under `wrk`.

The goal is repeatable local signal, not lab-grade numbers. Run on a quiet
machine and compare repeated runs before drawing conclusions.

## HTML generation (`html/`)

### Comparison: `html/compare.py`

Renders the same 50-row product page (autoescaping on everywhere) with:

- `stario @baked` — precompiled layout, slots spliced per call
- `stario` naive — full tree built per call
- `jinja2` — compiled template, autoescape
- `htpy`
- `dominate`
- `tdom` — Python 3.14 t-strings ([tdom](https://github.com/t-strings/tdom));
  naive inline template and Page/Row component functions

Times are best-of-N microseconds per full page render; lower is better.
Install competitor packages on demand, or run from a virtual environment that
already has them:

```bash
cd projects/stario
uv run --with dominate --with htpy --with jinja2 --with tdom benchmarks/html/compare.py
```

### Microbenchmarks: `html/micro.py`

Hot-path timings for stario only — tag construction with each attribute shape,
`@baked` splice calls (positional and keyword), and the render walk. Use these
to catch regressions when touching `tag.py`, `attributes.py`, `baked.py`, or
`render.py`. No extra dependencies:

```bash
cd projects/stario
uv run benchmarks/html/micro.py
```

## HTTP server (`server/`)

Compares Stario with native HTTP servers and ASGI framework stacks. The route
mix follows Cemrehan Çavdar's framework comparison:
https://cemrehancavdar.com/2026/02/10/framework-benchmark/

Each target runs its **own** server process — not as an ASGI backend for another
app (except the explicit ASGI stack rows below).

The runner keeps the setup explicit: one process per target, one load
generator, dedicated virtual environments, and separate ports so a stale
server cannot be reused by accident.

### Targets

**Stario (this checkout)**

| Target | Server |
| --- | --- |
| `stario` | Python httptools protocol |
| `stario-cython` | Cython llhttp protocol (`cython-core`) |

**Native HTTP servers**

| Target | Server |
| --- | --- |
| `socketify` | [Socketify.py](https://github.com/cirospaciari/socketify.py) (uWebSockets + libuv) |
| `robyn` | [Robyn](https://github.com/sparckles/Robyn) (Rust/Actix) |
| `granian-rsgi` | [Granian](https://github.com/emmett-framework/granian) RSGI (Rust, no framework) |
| `sanic` | [Sanic](https://sanic.dev/) (uvloop) |
| `django-bolt` | [Django-Bolt](https://bolt.farhana.li/) (Actix/Tokio) |

**ASGI framework stacks**

| Target | Stack |
| --- | --- |
| `blacksheep-granian` | BlackSheep + Granian ASGI |
| `blacksheep-uvicorn` | BlackSheep + Uvicorn |
| `fastapi` | FastAPI + Uvicorn + Pydantic |

### Benchmark shape

- One worker/process per target.
- Same paths: `/plaintext`, `/json`, `/user/42`, and `POST /validate`.
- Same `wrk` settings for every run.
- Each endpoint is measured multiple times (`RUNS`, default 7). The first
  `WARMUP` samples are discarded, then IQR outlier trimming is applied before
  reporting median requests/sec ± sample stdev.
- Stario response compression disabled with negative codec levels.
- JSON response bodies use `ujson` where the stack allows it.
- Access logging and OpenAPI docs disabled for competitors where configurable.

FastAPI uses Pydantic validation, matching the referenced benchmark. Stario,
BlackSheep, Sanic, Socketify, Robyn, and Granian RSGI validate the JSON body
manually. Django-Bolt uses msgspec structs.

### Requirements

- `uv`
- `wrk`
- Python compatible with Stario (3.14+)
- `libbrotli-dev` (or set `BROTLI_PKG_CONFIG`) for `stario-cython`

Targets that support uvloop use it where applicable (Stario, Sanic, Granian
RSGI, ASGI stacks). Socketify, Robyn, and Django-Bolt use their own native
runtimes (libuv, Rust, Tokio).

The runner creates dedicated virtual environments under
`benchmarks/server/.venvs/` with `uv venv` and `uv pip install`, then starts
each server from its own environment. The Stario targets install this checkout
as `stario @ file://...`. Use `REFRESH_ENVS=1` after changing dependencies or
upgrading framework versions.

### Run

```bash
cd projects/stario
benchmarks/server/run.sh
```

The default run benchmarks every target above.

- `DURATION=10s`
- `THREADS=2`
- `CONNECTIONS=128`
- `RUNS=7` measured samples per endpoint (plus `WARMUP=2` discarded warmup runs)
- `PORT=3000` as the base port
- one process or worker per target

Use those defaults when you want numbers that are easiest to compare with
other local runs. The generated `config.txt` records the exact settings for
that run. The summary groups results into **Stario**, **Native HTTP servers**,
and **ASGI framework stacks**.

Common options:

```bash
DURATION=30s THREADS=2 CONNECTIONS=128 RUNS=9 WARMUP=2 benchmarks/server/run.sh
benchmarks/server/run.sh stario stario-cython socketify robyn granian-rsgi
PORT=3999 benchmarks/server/run.sh
REFRESH_ENVS=1 benchmarks/server/run.sh
KEEP_RAW=1 benchmarks/server/run.sh
WRK=/path/to/wrk benchmarks/server/run.sh
BROTLI_PKG_CONFIG=/opt/brotli/lib/pkgconfig benchmarks/server/run.sh stario-cython
```

`PORT` is a base port, not a shared port. The runner assigns fixed offsets per
target in `TARGETS` order (`stario` on `PORT`, `stario-cython` on `PORT+1`,
`socketify` on `PORT+2`, and so on) and checks each port before starting a
server.

Each run writes a timestamped directory under `benchmarks/server/results/`:

- `summary.md` — grouped markdown tables.
- `config.txt` — run settings.

Successful runs keep only `summary.md` and `config.txt` by default. Use
`KEEP_RAW=1` to keep the per-endpoint `wrk` output and server logs. Failed
runs leave the logs in place so startup issues can be inspected.

The `POST /validate` benchmark uses `benchmarks/server/validate.lua` so every
endpoint runs through `wrk`.

The Lua file is intentionally small:

```lua
wrk.method = "POST"
wrk.body = '{"name":"Ada","age":42}'
wrk.headers["Content-Type"] = "application/json"
```
