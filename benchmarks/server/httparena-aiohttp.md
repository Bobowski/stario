# Stario vs aiohttp on HttpArena

HttpArena ([http-arena.com](https://www.http-arena.com/), Alpha Round,
results dated 2026-08-25) is the live public league table now that
TechEmpower Framework Benchmarks has shut down. **aiohttp is #1 Python
on the HTTP/1.1 composite.** Stario is not on the board. This note
says what that ranking actually measures, and what our own suite can
and cannot say about beating it.

Sources: [aiohttp entry](https://www.http-arena.com/frameworks/aiohttp/),
[implementation](https://github.com/MDA2AV/HttpArena/tree/main/frameworks/aiohttp),
our `baseline-20260824.md` / `positioning.md` / `go-comparison.md`.

## What “#1 Python” means

It is **not** “highest plaintext RPS.” HttpArena scores a **composite**:
each HTTP/1.1 profile is worth 100 to the field leader, the entry sums
those normalized scores, then loses **2.5% per missing** framework
surface (routing, middleware, request object, response builder).
Incomplete suites score zero on the profiles they skip.

aiohttp’s page: **composite 240**, HTTP/1.1 rank **29 of 61** frameworks
overall (same number as Spring Boot), **gold “within Python HTTP/1.1”
(1 of 8)**. Completeness **4/4**, standard configuration, flagship.

The Python HTTP/1.1 board (Alpha Round):

| Entry | Composite | Notes |
| --- | ---: | --- |
| **aiohttp** | **240** | own server + uvloop, 1 fork/core, `SO_REUSEPORT` |
| litestar | 236 | uvicorn + msgspec |
| starlette | 225 | uvicorn |
| mq-bridge-py | 222 | Class A: Rust HTTP, Python dispatch, completeness 2/4 |
| fastapi | 190 | uvicorn |
| sanic | 158 | **faster than aiohttp on the profiles it ran**; incomplete + tuned |
| flask | 106 | |
| bottle | 98 | |
| slimeweb | 96 | |
| robyn | 59 | incomplete; pipelined looks broken (~16M) |
| django | 44 | |

Sanic’s own server + ujson + httptools **beats aiohttp on raw
connection and JSON**, then drops the composite because it skipped
TLS, static, DB, CRUD, and the API-4/API-16 cliffs:

| Profile (best conn count) | aiohttp | sanic | starlette | fastapi |
| --- | ---: | ---: | ---: | ---: |
| Baseline 4,096 | 590,654 | **611,737** | 399,413 | 298,664 |
| Pipelined 512 (not scored) | 1,171,337 | **1,461,670** | 492,899 | 344,457 |
| JSON 4,096 | 286,695 | **344,340** | 225,181 | 155,615 |
| JSON + gzip 4,096 | 101,899 | **172,836** | 130,650 | 101,178 |
| Upload 32 (20 MB) | 1,610 | 1,814 | **2,132** | 2,117 |
| Static 1,024 | **94,209** | — | 23,668 | 23,482 |
| Async DB 1,024 | **102,119** | — | 88,740 | 75,930 |
| Memory (baseline 512) | **539 MiB** | 1.6 GiB | 3.5 GiB | 4.9 GiB |

aiohttp wins Python by **shipping the whole standard-mode suite** and
by being cheap (half a gig at 64 workers). It does not win every
profile.

## Their aiohttp entry is the same deploy we already use

From `frameworks/aiohttp/app.py`: aiohttp 3.14, uvloop, **one forked
worker per container CPU**, each bind with **`reuse_port=True`**,
routing through `app.router`, JSON through `web.json_response`, gzip
through `response.enable_compression()`, uploads via
`request.content.iter_any()`, static via `web.FileResponse` (disk
every request), TLS on 8081 when `/certs` is mounted, Postgres/Redis
only when the harness injects them.

That is the same shape as `stario-n` / `stario-cython-n`
(`STARIO_REUSE_PORT=1`, N processes). aiohttp is a **Class B**
framework in `positioning.md` terms — real router, request/response,
middleware, compression — not Granian-with-`if path`.

The scored HTTP/1.1 work is heavier than our 10 routes:

| Profile | What it does |
| --- | --- |
| baseline | mixed GET/POST, query ints summed, optional POST body int |
| short-lived | same, connection closed after 10 requests |
| json | slice a dataset, multiply fields, serialize |
| json-comp / json-tls | same + gzip/br and/or TLS |
| upload | 20 MB body, return byte count |
| static / static-tls | 20 files off disk |
| async-db | async Postgres range scan |
| crud | REST + optional Redis cache-aside |
| api-4 / api-16 | mixed baseline+JSON+db pinned to 4 / 16 CPUs |

Hardware: **64-core Threadripper PRO 3995WX**, container pinned to
dedicated cores, Docker host net, **gcannon** (not wrk), best of 3,
default 5s. **Do not compare their 591k baseline to our 294k
4-vCPU plaintext.** Different iron, client, payload, and duration.

## What our suite can say

We have never run aiohttp. We *have* run three stacks that also sit
on HttpArena: FastAPI, Sanic, Robyn (plus Django-Bolt locally, Django
on their board).

### Shared peers, two machines

| Stack | Our 1-worker plaintext | HttpArena baseline 4,096 |
| --- | ---: | ---: |
| Stario Cython | **120,378** | not entered |
| Sanic | 52,746 | **611,737** (tuned, own server) |
| FastAPI + Uvicorn | 47,533 | 298,664 |
| Robyn | 25,416 | 430,749 |

Locally Sanic is only **~1.1×** FastAPI. On HttpArena, tuned Sanic is
**~2.0×** FastAPI — the same ratio as **our Cython vs FastAPI
(~2.5×)**. aiohttp is **~2.0×** FastAPI on their baseline.

So the honest transfer is:

- **Against FastAPI/Starlette/Uvicorn:** we should still look clearly
  faster. That is the Class B claim we already make.
- **Against aiohttp / tuned Sanic on connection+JSON:** expect a
  **same-band fight**, maybe a modest edge (~1.0–1.3×) if the Cython
  protocol tax stays. **Not** a 2× headline. Their Sanic already
  matches aiohttp on those profiles; our local Sanic is a weaker
  reference than theirs.
- **Against Robyn:** both suites agree we (and aiohttp) are far ahead
  on real request work. Their Robyn pipelined row is not usable.

### Bodies and memory

HttpArena’s 20 MB upload is I/O-bound. FastAPI/Starlette/Litestar
**beat** aiohttp there (~2.1k vs 1.6k). Our 2 MB stream win vs ASGI
does not automatically become an aiohttp win on their upload.

aiohttp’s **539 MiB** at 64 workers is the Python efficiency story.
ASGI entries sit at 3–5 GiB. Cython should land nearer aiohttp than
FastAPI if we submit; that is one place we can actually look good
even if RPS is a coin flip.

### 4-core fill-the-box (ours only)

`go-comparison.md`, 4 vCPU, `SO_REUSEPORT`: Cython **294k** plaintext /
**252k** validate. That is how Stario scales, and it is the same
knob aiohttp already uses. It is **not** a number you can put next
to their 591k.

## Feature gaps if we entered tomorrow

HttpArena standard mode wants framework APIs, not a toy `if path`.
We already have routing, scoped middleware, request, response, gzip /
brotli / zstd, streamed bodies, and `reuse_port`. Gaps vs their
HTTP/1.1 family:

| Surface | Stario today | Needed for a fair entry |
| --- | --- | --- |
| TLS listener | not a product feature | `ssl.SSLContext` on the listen socket (asyncio can) |
| Static files | no `FileResponse` | read-from-disk handler; they forbid preloading |
| Postgres / Redis | none | app-level `asyncpg` / redis, same as aiohttp |
| WebSocket | Upgrade → 400 | skip the WS family |
| HTTP/2, HTTP/3, gRPC | no | skip those families |

A complete **HTTP/1.1-only** entry with completeness 4/4 is enough to
compete for #1 Python. Missing TLS/static/db is how Sanic ended at
158 instead of ~240. Do not submit a plaintext-only app.

`mq-bridge-py` (1.2M baseline) is **Class A** (Rust accept, 2/4). Do
not treat it as the Python framework to beat. Same rule as Granian.

## Lines that are safe

**True today:** aiohttp is the best *complete, standard-mode* Python
framework on HttpArena. It is the Class B opponent the “fastest
Python framework” claim has to beat in public. We are not on that
board, so we have **no HttpArena rank**.

**True from our suite:** vs the ASGI frameworks HttpArena also runs
(FastAPI, Starlette via FastAPI, Sanic-as-we-ran-it), Cython is
~2–2.5× on honest 1-worker routes. That still supports the Class B
headline **on our machine, our routes**.

**Do not say:** “We are faster than aiohttp.” Different hardware,
different client, different payloads, and their tuned Sanic already
ties them on the closest profiles.

**Do not say:** “aiohttp is slow.” It is mid-pack overall (240) and
the efficiency leader in Python. The Python ceiling on this board is
mid-pack; #1 Python is the prize, not catching C# / Bun / actix.

**To actually know:** add `frameworks/stario/` to
[MDA2AV/HttpArena](https://github.com/MDA2AV/HttpArena) — Dockerfile,
the scored HTTP/1.1 endpoints, workers + `SO_REUSEPORT`, standard-mode
APIs — and let their machine publish the row.
