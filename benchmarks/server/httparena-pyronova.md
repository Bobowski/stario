# Stario vs Pyronova

[Pyronova](https://github.com/leocaolab/pyronova) is the first
Rust-core Python framework that is actually fast. It is on HttpArena
as **experimental + tuned**, not on the flagship Python gold that
aiohttp holds. We have not run it on this machine. This note says
what their numbers measure, which class they belong in, and what we
can say without inventing a rank.

Sources: [repo](https://github.com/leocaolab/pyronova),
[HttpArena entry](https://www.http-arena.com/frameworks/pyronova/),
[Arena app](https://github.com/MDA2AV/HttpArena/blob/main/frameworks/pyronova/app.py),
their [benchmark-14](https://github.com/leocaolab/pyronova/blob/main/benchmarks/benchmark-14-linux.md)
(Ryzen 7 7840HS, 8C/16T), our `positioning.md` / `httparena-aiohttp.md`.

## What it is

Rust **hyper + tokio + rustls + mimalloc** accept loop. Python
handlers run in **PEP 684 sub-interpreters** (one GIL each) inside
**one process**. Decorator router, `Request` / `Response`,
before/after hooks, gzip/brotli, TLS, HTTP/2, WebSocket, SSE, static
files, sqlx Postgres. Completeness **4/4**.

That is Robyn’s product shape with Granian/fasthttp metal. It is
**not** aiohttp (Python protocol). It is **not** Granian (no
framework). HttpArena’s own tags: `type: experimental`, `mode: tuned`.

## HttpArena (same 64-core box as aiohttp)

| Profile | Pyronova | aiohttp | sanic (tuned) |
| --- | ---: | ---: | ---: |
| Composite (H1) | **431** (experimental league, 2 of 2) | 240 (flagship gold) | 158 |
| Baseline 4,096 | **825,534** | 590,654 | 611,737 |
| JSON 4,096 | **723,144** | 286,695 | 344,340 |
| JSON + gzip 4,096 | **463,958** | 101,899 | 172,836 |
| JSON TLS 4,096 | **780,948** | 240,408 | — |
| Upload 32 (20 MB) | **2,766** | 1,610 | 1,814 |
| Async DB 1,024 | 23,302 | **102,119** | — |
| CRUD 4,096 | 12,019 | **41,864** | — |
| API-16 | **51,624** | 45,702 | — |
| Memory (baseline 512) | 860 MiB | **539 MiB** | 1.6 GiB |
| Pipelined 512 (not scored) | 4.10M | 1.17M | 1.46M |

Baseline **is** a real handler (sum query ints, optional POST body).
**1.4× aiohttp / 1.35× Sanic** on that profile is the Rust accept
loop on the same iron. Treat that as the honest Pyronova-vs-Python
gap on connection work.

JSON **is not** honest. Their Arena app keeps a per-worker
`_JSON_CACHE` of pre-serialized bytes (cap 256 keys) and documents
it. `add_fast_response("GET", "/pipeline", b"ok")` skips Python
entirely. gzip level=1 / brotli quality=0. Upload uses
`req.stream.drain_count()` entirely in Rust. That is why the entry
is **tuned**. Do not quote 723k JSON or 4.1M pipelined as a
framework result.

gRPC ~5M looks like a Rust-native stub, not a Python handler race.
HTTP/2 is the same Hyper engine; we do not have h2.

**Database is a loss:** 23k async-db vs aiohttp 102k. CRUD 12k vs
42k. sqlx in Rust, but their CRUD handlers are `gil=True` on the
main interpreter.

## Their own 8C/16T report (not our box)

`wrk -t4 -c100 -d10s`, 16 sub-interpreters + 16 io_workers, one
process. GET `/` **429k**, `/json` **405k**, `/user/42` **395k**,
POST `/echo` **372k**. vs Robyn 16 proc × 2 workers: **2.6–2.7×**
and 189 MB vs 583 MB. 300s soak at 401k, 120M requests, zero
errors.

That is **fill-the-box on a 16-thread laptop**, not 1-worker. Do
not put 429k next to our 120k Cython 1-worker row. The nearer
published number we have is Cython **×4 on 4 vCPU = 294k**
plaintext (`go-comparison.md`). They have ~2× the threads and
print 1.46× the RPS. Per-core they look **same-band**, not 3×.

They also advertise `@cached_json(ttl=...)` (68k → 336k on a
100-row payload) and Rust `serde` for dict returns. Our handler
policy forbids both.

## Class

| | Pyronova | Stario Cython | aiohttp |
| --- | --- | --- | --- |
| HTTP engine | Rust Hyper/Tokio | Cython llhttp + asyncio | aiohttp + uvloop |
| Scale knob | sub-interpreters in 1 process | N processes, `SO_REUSEPORT` | N forks, `SO_REUSEPORT` |
| Handler language | Python (`def` / `async def`) | Python async | Python async |
| HttpArena board | experimental / tuned | not entered | flagship / standard / #1 Python |

Put Pyronova with **Class A** in `positioning.md` (native accept +
Python callbacks), next to Granian / Socketify / Robyn-as-metal —
**not** in the Class B 2× table with FastAPI / Sanic / BlackSheep /
aiohttp.

It has more framework surface than Granian or Socketify (router,
middleware hooks, WS, SSE, static, compression, TLS). That does not
move the HTTP engine into Python. Robyn and Django-Bolt have a
similar story and were *slow*; Pyronova is the one that made the
story fast. Beating Robyn 4.7× on our suite does not beat Pyronova.

C extensions (orjson, pydantic, numpy, sqlalchemy) need `gil=True`
and fall back to the main interpreter. Fast Arena/README routes
avoid that. Real apps that import those packages leave the 400k
path.

## What we can say

**True:** Pyronova is the fastest *Rust-core Python* entry on
HttpArena. On an honest baseline it is ~1.4× aiohttp on that
64-core box. Their self-published 429k vs Robyn is consistent with
our Robyn being the slow one, not with Stario being the slow one.

**True from our suite:** vs Python-protocol Class B (aiohttp’s
peers: FastAPI, Sanic, BlackSheep, Stario Python) Cython is still
~2–2.5× on this machine. That claim does not include Pyronova.

**Estimate, not a measurement:** Stario’s scale model is aiohttp’s
(fork + `SO_REUSEPORT`), not Hyper-in-process. On HttpArena we
would expect to land near aiohttp/Sanic on connection (~600–750k
if the FastAPI transfer holds), **behind** Pyronova’s 826k
baseline, and far behind their cached JSON. On 4 vCPU, 294k
Cython vs a hypothetical 16-worker Pyronova is unknown; their
8C/16T 429k suggests we would not be embarrassed per core and
would not win a fill-the-box Rust race.

**Do not say:** “Faster than Pyronova.” We have no same-machine
row, and their Arena JSON/pipeline numbers are tuned.

**Do not say:** “Pyronova is just Socketify.” More framework, real
HttpArena completeness, production CLI, WS/H2. Say “Rust HTTP
engine, Python handlers, experimental+tuned on the public board.”

**Do not use their 429k or 723k in a Class B slide.**

**To actually know:** add an honest `apps/pyronova_app.py` (same 10
routes, per-request JSON, no cache, no `add_fast_response`) and
run `pyronova` with `PYRONOVA_WORKERS=$nproc` next to
`stario-cython-n` and a production aiohttp. Until that run exists,
the only fair public number is HttpArena baseline 826k vs aiohttp
591k.
