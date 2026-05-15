# Stario benchmark

This directory contains the small local benchmark used to compare the current
Stario checkout with a few Python web framework/server combinations. The route
mix follows Cemrehan Çavdar's framework comparison:
https://cemrehancavdar.com/2026/02/10/framework-benchmark/

The goal is repeatable local signal, not a lab-grade benchmark. The runner keeps
the setup explicit: one process per target, one load generator, dedicated
virtual environments, and separate ports so a stale server cannot be reused by
accident.

Targets:

- `stario` — this checkout with no-op tracing
- `fastapi` — FastAPI on one Uvicorn worker
- `blacksheep-uvicorn` — BlackSheep on one Uvicorn worker
- `blacksheep-granian` — BlackSheep on one Granian worker
- `sanic` — Sanic in one process

## Benchmark shape

- One worker/process per framework.
- Same paths: `/plaintext`, `/json`, `/user/42`, and `POST /validate`.
- Same `wrk` settings for every run.
- Stario response compression disabled with negative codec levels.
- JSON response bodies use `ujson` for Stario and FastAPI to match Sanic's `ujson` dependency more closely.
- Stario runs with `--tracer noop`; FastAPI, BlackSheep/Uvicorn, BlackSheep/Granian, and Sanic run with access logging disabled.

FastAPI uses Pydantic validation, matching the referenced benchmark. Stario,
BlackSheep, and Sanic validate the JSON body manually because they do not bundle
a Pydantic-style request validation layer.

The 3.3 vs 3.2 release comparison in the blog was a one-off release check. This
repo benchmark only compares the current Stario checkout with other frameworks.

## Requirements

- `uv`
- `wrk`
- Python compatible with Stario (3.14+)

All targets run on `uvloop`: Stario uses `--loop uvloop`, FastAPI/Uvicorn
uses `--loop uvloop`, and Sanic uses its uvloop-backed default when installed.

The runner creates dedicated virtual environments under `benchmark/.venvs/`
with `uv venv` and `uv pip install`, then starts each server from its own
environment. The Stario target installs this checkout as `stario @ file://...`.
Use `REFRESH_ENVS=1` after changing dependencies or upgrading framework versions.

## Run

```bash
cd projects/stario
benchmark/run.sh
```

The default run is the standard comparison shape:

- `DURATION=10s`
- `THREADS=2`
- `CONNECTIONS=128`
- `PORT=3000` as the base port
- one process or worker per target

Use those defaults when you want numbers that are easiest to compare with other
local runs. The generated `config.txt` records the exact settings for that run.

Common options:

```bash
DURATION=30s THREADS=2 CONNECTIONS=128 benchmark/run.sh
benchmark/run.sh stario blacksheep-granian
PORT=3999 benchmark/run.sh
REFRESH_ENVS=1 benchmark/run.sh
KEEP_RAW=1 benchmark/run.sh
```

`PORT` is a base port, not a shared port. The runner assigns fixed offsets per
target (`stario` on `PORT`, `fastapi` on `PORT+1`, and so on) and checks each
port before starting a server.

Each run writes a timestamped directory under `benchmark/results/`:

- `summary.md` — markdown tables suitable for copying into notes or docs.
- `config.txt` — run settings.

Successful runs keep only `summary.md` and `config.txt` by default. Use
`KEEP_RAW=1` to keep the per-endpoint `wrk` output and server logs. Failed runs
leave the logs in place so startup issues can be inspected.

The `POST /validate` benchmark uses `benchmark/validate.lua` so every endpoint
runs through `wrk`.

The Lua file is intentionally small:

```lua
wrk.method = "POST"
wrk.body = '{"name":"Ada","age":42}'
wrk.headers["Content-Type"] = "application/json"
```

## Notes

Run on a quiet machine. Keep the generated `config.txt` with any published
numbers, and compare repeated runs before drawing conclusions.
