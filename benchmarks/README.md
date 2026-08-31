# Stario benchmarks

Three suites, one per layer we care about:

- `html/` — HTML generation speed: stario against other Python renderers, plus
  microbenchmarks for stario's own hot paths.
- `server/` — end-to-end HTTP throughput: Stario against native Python HTTP
  servers and ASGI framework stacks under `wrk`.
- `headers_micro.py` — Cython request-header storage tradeoffs: eager dict,
  current-style lazy dict, direct arena scans, and adaptive promotion.
- `query_micro.py` — Cython query `get` scan-on-demand vs eager parse-all.

The goal is repeatable local signal, not lab-grade numbers. Run on a quiet
machine and compare repeated runs before drawing conclusions.

## Request-header storage (`headers_micro.py`)

This benchmark informs the Cython protocol's request-header representation. It
models pooled exchange state and compares complete parse-plus-handler-access
cycles for 8, 16, and 32 fields. Workloads include no application reads, one
or several arbitrary reads, repeated reads, and single/repeated Cookie fields.

The Cython backend verifies that every representation returns identical values
before measuring it. Header names are normalized and hashed while entering the
arena; lookup queries use normalized bytes, matching the protocol's internal
header path.

```bash
PYTHONPATH=src:. .venv/bin/python benchmarks/headers_micro.py
```

Defaults are 100,000 logical requests and seven repeats per case. Override
them or retain machine-readable results with:

```bash
HEADERS_BENCH_ITERATIONS=200000 \
HEADERS_BENCH_REPEATS=9 \
HEADERS_BENCH_JSON=/tmp/headers-micro.json \
PYTHONPATH=src:. .venv/bin/python benchmarks/headers_micro.py
```

## Query scan vs eager (`query_micro.py`)

Compares production `ParsedQuery.get` / `getlist` (walk C pairs, decode only
matches) with `_get_eager` (decode every name and value, then list-scan). Same
decoder, pooled `ParsedQuery.__init__(raw)` per request.

```bash
PYTHONPATH=src:. .venv/bin/python benchmarks/query_micro.py
```

Defaults are 80,000 requests and seven repeats. Latest numbers:
[`query-micro-20260831.md`](query-micro-20260831.md).

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
- Same read paths: `/plaintext`, `/json`, `/user/42`.
- Same upload/body paths (see **Endpoint tiers** below).
- Same `wrk` settings per tier; large uploads use fewer connections
  (`UPLOAD_CONNECTIONS`, default 32) than read-heavy cases (`CONNECTIONS`,
  default 128).
- Each endpoint is measured multiple times (`RUNS`, default 7). The first
  `WARMUP` samples are discarded, then IQR outlier trimming is applied before
  reporting median requests/sec ± sample stdev.
- Stario response compression disabled with negative codec levels.
- JSON response bodies use `ujson` where the stack allows it.
- Access logging and OpenAPI docs disabled for competitors where configurable.

FastAPI uses Pydantic validation, matching the referenced benchmark. Stario,
BlackSheep, Sanic, Socketify, Robyn, and Granian RSGI validate the JSON body
manually. Django-Bolt uses msgspec structs.

### Endpoint tiers

| Tier | Endpoints | Paths |
| --- | --- | --- |
| **read** | `plaintext`, `json`, `params` | `GET /plaintext`, `GET /json`, `GET /user/42` |
| **upload** | see below | POST body / multipart cases |
| **all** | read + upload | default |

Upload endpoints (all targets implement the same semantics):

| Endpoint key | Path | Client payload | Server behavior |
| --- | --- | --- | --- |
| `validate` | `POST /validate` | JSON `{"name":"Ada","age":42}` | Validate fields, return JSON |
| `post-form` | `POST /form` | urlencoded form | Read body, `204 No Content` |
| `post-json-1k` | `POST /echo/json` | 1 KB JSON | Buffered read, return `{"bytes": N}` |
| `post-octet-64k` | `POST /ingest/64k` | 64 KB octet stream | Buffered read |
| `post-octet-2m` | `POST /ingest/2m` | 2 MB octet stream | Buffered read |
| `post-stream-2m` | `POST /ingest/stream/2m` | 2 MB octet stream | Streaming read (where supported) |
| `multipart-2m` | `POST /upload` | 2 MB multipart file | Read multipart/raw body |

Binary fixtures live under `benchmarks/server/fixtures/` (generated on demand,
gitignored). Lua scripts under `benchmarks/server/scripts/` load those fixtures.

Robyn and Django-Bolt buffer the full body on the stream route (no request
streaming API). All other targets use chunked streaming reads on
`POST /ingest/stream/2m`.

### Route parity

Every app exposes the same paths and response shapes:

| Route | Method | Response |
| --- | --- | --- |
| `/plaintext` | GET | `Hello, World!` |
| `/json` | GET | `{"message":"Hello, World!"}` |
| `/user/{id}` | GET | `{"id":"…","name":"User …"}` |
| `/validate` | POST | `validate_fields()` → JSON + status |
| `/form` | POST | read body → `204` |
| `/echo/json` | POST | read body → `{"bytes":N}` |
| `/ingest/64k`, `/ingest/2m` | POST | buffered read → `{"bytes":N}` |
| `/ingest/stream/2m` | POST | stream read → `{"bytes":N}` (buffered on Robyn/Django-Bolt) |
| `/upload` | POST | read raw body → `{"bytes":N}` |

Validation uses `apps.common.validate_fields()` on every target. JSON responses
use `ujson` where the stack allows it.

### Handler policy (no static responses)

Benchmark handlers must **compute every response on each request**: build dicts,
run `validate_fields`, call `ujson.dumps` / framework JSON helpers, and construct
`Response` objects per call. Do **not**:

- Reuse a single `Response` / `PlainTextResponse` instance across requests
- Pre-serialize JSON at import time and return cached bytes
- Skip validation or serialization because the wrk payload is fixed

Framework-specific routing or server tuning is fine when output bytes and status
codes stay identical. The goal is to measure handler + serialization work, not
wire throughput of prebuilt buffers.

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
- `CONNECTIONS=128` (read-heavy and small upload cases)
- `UPLOAD_CONNECTIONS=32` (64 KB / 2 MB upload cases)
- `RUNS=7` measured samples per endpoint (plus `WARMUP=2` discarded warmup runs)
- `ENDPOINT_TIER=all|read|upload` (default `all`)
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
ENDPOINT_TIER=upload benchmarks/server/run.sh
ENDPOINTS=validate,post-form,post-json-1k benchmarks/server/run.sh stario
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

A committed reference baseline (hardware, methodology, Python vs Cython tables)
lives at `benchmarks/server/baseline-20260827.md`. Sync-handler capture vs
Granian and others: `benchmarks/server/baseline-20260829.md`. Timestamped
`results/` dirs remain gitignored.

Successful runs keep only `summary.md` and `config.txt` by default. Use
`KEEP_RAW=1` to keep the per-endpoint `wrk` output and server logs. Failed
runs leave the logs in place so startup issues can be inspected.

POST endpoints use small Lua scripts under `benchmarks/server/` so every case runs
through `wrk`. `validate.lua` is the simplest example:

```lua
wrk.method = "POST"
wrk.body = '{"name":"Ada","age":42}'
wrk.headers["Content-Type"] = "application/json"
```

Larger payloads load binary fixtures from `benchmarks/server/fixtures/` (see
`scripts/post-octet-2m.lua`, `scripts/multipart-2m.lua`, etc.).
