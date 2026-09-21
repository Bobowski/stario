# Stario benchmarks

Three suites, one per layer we care about:

- `html/` — HTML generation speed: stario against other Python renderers, plus
  microbenchmarks for stario's own hot paths.
- `server/` — end-to-end HTTP throughput: Stario against native Python HTTP
  servers and ASGI framework stacks under `wrk`.
- `headers_micro.py` — Cython request-header storage tradeoffs: eager dict,
  current-style lazy dict, direct arena scans, and adaptive promotion.
- `query_micro.py` — Cython query `get` / `getlist` (production C-span index).

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

## Query get (`query_micro.py`)

Times production `ParsedQuery.get` / `getlist` (copy query, index C spans,
memcmp names, decode the value). Pooled `ParsedQuery.__init__(raw)` per request.

```bash
PYTHONPATH=src:. .venv/bin/python benchmarks/query_micro.py
```

Defaults are 80,000 requests and seven repeats. Older comparison notes:
[`query-micro-index-20260831.md`](query-micro-index-20260831.md) (index vs
scan vs eager) and [`query-micro-20260831.md`](query-micro-20260831.md).

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
| `stario` | Cython llhttp + nghttp2 (`stario serve`) |
| `stario-cython` | Same runtime via `python -m stario_cython` |

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
| `fastapi` | FastAPI + Uvicorn |
| `falcon` | Falcon ASGI + Uvicorn |

### Benchmark shape

- One worker/process per target.
- Same paths and response bytes per endpoint (see **Endpoint tiers**).
- Same `wrk` settings per tier; large uploads use fewer connections
  (`UPLOAD_CONNECTIONS`, default 32) than GET cases (`CONNECTIONS`, default 128).
- Each endpoint is measured multiple times (`RUNS`, default 7). The first
  `WARMUP` samples are discarded, then IQR outlier trimming is applied before
  reporting median requests/sec ± sample stdev.
- Stario response compression disabled with negative codec levels.
- Access logging and OpenAPI docs disabled for competitors where configurable.

The progression follows a normal app: a static page, then a request that actually
reads incoming fields, then async body work from a small JSON POST up through
buffered, streamed, and multipart uploads. FastAPI routes stay on Starlette
`Route` (no Pydantic models). Incoming JSON is parsed with `ujson` where the
stack allows it; **responses are always plain text**, so JSON serializers cannot
win a case.

### Endpoint tiers

| Tier | Endpoints | What it measures |
| --- | --- | --- |
| **static** | `plaintext` | Prebuilt body, fixed method + path |
| **request** | `request` | Path param + query + header, interpolated text |
| **read** | static + request | Both GET cases |
| **upload** | see below | Async body work, small → large |
| **app** | request + upload | Everything except the static page |
| **all** | read + upload | default |

GET endpoints:

| Endpoint key | Path | Client | Server reads | Response |
| --- | --- | --- | --- | --- |
| `plaintext` | `GET /plaintext` | Fixed URL | nothing | prebuilt `Hello, World!` |
| `request` | `GET /user/{id}` | wrk cycles 4096 ids, `q`, and `X-Request-Id` | path `user_id`, query `q`, header `x-request-id` | `user={id} q={q} x={header}` |

`request` uses a working set of 4096 unique paths, larger than Stario's
`find_handler` LRU (1024). Query `q=term{N}` and header `X-Request-Id: h{N}`
vary with the same counter. The body is a single interpolated line so the
checkpoint is that those values appear; there is no JSON encode step.

Upload endpoints (every handler `await`s `asyncio.sleep(0)` after reading, so
the case is an async operation, not a sync function declared `async def`):

| Endpoint key | Path | Client payload | Server behavior | Response |
| --- | --- | --- | --- | --- |
| `json-small` | `POST /echo` | JSON `{"name":"Ada","age":42}` | Parse object, `await` | `name=Ada age=42` |
| `post-octet-64k` | `POST /ingest/64k` | 64 KB octet stream | Buffered read, `await` | `bytes=N` |
| `post-octet-2m` | `POST /ingest/2m` | 2 MB octet stream | Buffered read, `await` | `bytes=N` |
| `post-stream-2m` | `POST /ingest/stream/2m` | 2 MB octet stream | Streaming read, `await` | `bytes=N` |
| `multipart-2m` | `POST /upload` | 2 MB multipart file | Read body, `await` | `bytes=N` |

`asyncio.sleep(0)` yields once with no timer delay. It does not cap throughput
at `1 / delay`. Robyn and Django-Bolt buffer the full body on the stream route
(no request streaming API). All other targets use chunked streaming reads on
`POST /ingest/stream/2m`.

Binary fixtures live under `benchmarks/server/fixtures/` (generated on demand,
gitignored). Lua scripts under `benchmarks/server/scripts/` drive wrk.

### Route parity

Every app exposes the same paths and response lines:

| Route | Method | Response |
| --- | --- | --- |
| `/plaintext` | GET | prebuilt `Hello, World!` |
| `/user/{id}` | GET | `user={id} q={q} x={x-request-id}` |
| `/echo` | POST | `name={name} age={age}` after `await` |
| `/ingest/64k`, `/ingest/2m` | POST | `bytes={n}` after buffered read + `await` |
| `/ingest/stream/2m` | POST | `bytes={n}` after stream read + `await` |
| `/upload` | POST | `bytes={n}` after body read + `await` |

`apps.common.request_line` / `json_echo_line` / `bytes_line` are the exact
strings. The GET `request` case reads:

- path parameter `user_id`
- query parameter `q`
- header `x-request-id`

### Handler policy

**Static** (`plaintext`): return a prebuilt body. The app side may be fully
optimized. Do not serialize or allocate payload bytes per request. A new
`Response` wrapper is fine when the framework requires one.

**Request fields** (`request`): on every request, read the path param, query
`q`, and `x-request-id`, and interpolate them into `request_line`. wrk must
vary all three (`scripts/get-user.lua`). Do not JSON-encode the result.

**Uploads**: read the body (or stream it) every time, then `await` via
`apps.common.yield_once` (`asyncio.sleep(0)`). `json-small` parses the object
and interpolates `name` and `age`; larger cases report `bytes={n}`. Declaring
`async def` and never awaiting is not this case.

Framework-specific routing or server tuning is fine when output bytes and
status codes stay identical.

Older committed baselines (`baseline-20260827.md` and later) used a single
`/user/42` URL and JSON responses. They are not comparable to this suite.

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
- `ENDPOINT_TIER=all|static|request|read|upload|app` (default `all`)
- `PORT=3000` as the base port
- one process or worker per target

Use those defaults when you want numbers that are easiest to compare with
other local runs. The generated `config.txt` records the exact settings for
that run. The summary groups results into **Stario**, **Native HTTP servers**,
and **ASGI framework stacks**, with Static / Request fields / Upload tables.

Common options:

```bash
DURATION=30s THREADS=2 CONNECTIONS=128 RUNS=9 WARMUP=2 benchmarks/server/run.sh
benchmarks/server/run.sh stario stario-cython socketify robyn granian-rsgi
ENDPOINT_TIER=upload benchmarks/server/run.sh
ENDPOINT_TIER=static benchmarks/server/run.sh stario stario-cython
ENDPOINT_TIER=request benchmarks/server/run.sh
ENDPOINTS=json-small,post-octet-64k benchmarks/server/run.sh stario
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
lives at `benchmarks/server/baseline-20260827.md`. Framework comparison on the
reshaped suite (static / request fields / async uploads):
[`baseline-20260921.md`](server/baseline-20260921.md).
`STARIO_THREADS` 1 vs 2 vs 4 on 3.14t (`SO_REUSEPORT`, N full servers):
[`baseline-threads-reuseport-20260921.md`](server/baseline-threads-reuseport-20260921.md).
Earlier acceptor-handoff capture (same host, superseded runtime):
[`baseline-threads-20260921.md`](server/baseline-threads-20260921.md). Sync-handler capture vs
Granian and others: `benchmarks/server/baseline-20260829.md`. Timestamped
`results/` dirs remain gitignored.

Successful runs keep only `summary.md` and `config.txt` by default. Use
`KEEP_RAW=1` to keep the per-endpoint `wrk` output and server logs. Failed
runs leave the logs in place so startup issues can be inspected.

`wrk` Lua scripts live under `benchmarks/server/` and `benchmarks/server/scripts/`.
GET `request` varies path, query, and `X-Request-Id` (`scripts/get-user.lua`).
POST cases set method, body, and content type. `validate.lua` is the small JSON:

```lua
wrk.method = "POST"
wrk.body = '{"name":"Ada","age":42}'
wrk.headers["Content-Type"] = "application/json"
```

Larger payloads load binary fixtures from `benchmarks/server/fixtures/` (see
`scripts/post-octet-2m.lua`, `scripts/multipart-2m.lua`, etc.).
