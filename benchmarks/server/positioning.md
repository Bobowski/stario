# Claiming “fastest Python HTTP stack”

Stario can say it is the **fastest Python server framework we measured** —
not “faster than every native accept loop on earth.” Split the field into
two classes, then only fight the class that ships the same product.

## Two classes

| Class | What it is | Verdict |
| --- | --- | --- |
| **A. Thin native servers** | Accept loop + maybe a toy `app.get`. No framework model. | **1–1 is fine** |
| **B. Python frameworks** | Routing, params, middleware-shaped apps, JSON/forms/uploads, a real handler model | **Must be ~2× or more** |

**Class A** — do not use these as the headline opponent:

- **Granian RSGI** — Rust server, no router. Our bench app is `if path ==`.
  That is uvicorn-without-uvicorn, not FastAPI.
- **Socketify** — uWebSockets + a micro `app.get` / `:param` table. No
  host routing, no middleware tree, no HTML/SSE stack. Hello-world is a
  C++ server with a Python callback.
- **Pyronova** — Hyper + Tokio + PEP 684 sub-interpreters. Real
  decorator router, WS/H2/SSE, completeness 4/4. The HTTP engine is
  still Rust. HttpArena marks the entry **experimental + tuned**
  (cached JSON bytes, Rust `add_fast_response` for `/pipeline`).
  Details: `httparena-pyronova.md`.

Socketify *has* path params. Treat it as Class A anyway: it is a native
server with a route table, not a framework you would build a product in.
Pyronova has more framework than Socketify; it still does not belong
in the Class B 2× table.

**Class B** — this is the claim:

FastAPI, Sanic, BlackSheep, Robyn, Django-Bolt, and **Stario Python**
(httptools, no Cython). Same 10 routes, params, JSON, validate, form,
buffered + streamed bodies, multipart. Handlers compute every response
(see `benchmarks/README.md`).

BlackSheep **on Granian** is Class A’s accept loop wearing a Class B
coat. Quote BlackSheep **on Uvicorn** when you say “Python framework.”
Quote BlackSheep+Granian only as “framework + Rust server.”

## Proof (committed 1-worker suite)

Source: `baseline-20260824.md`, same machine class, `wrk -t2 -c128`,
honest handlers. Cython plaintext anchor **120k** (isolated re-run here
**118k**; the 97k long-suite row was noise).

### Class B — frameworks (headline)

| Stack | Plaintext | Validate | vs Cython plaintext |
| --- | ---: | ---: | ---: |
| **Stario Cython** | **120,378** | **82,402** | 1.00× |
| Stario Python | 63,268 | 50,417 | **1.9×** |
| BlackSheep + Uvicorn | 59,195 | 50,554 | **2.0×** |
| Sanic | 52,746 | 34,731 | **2.3×** |
| FastAPI + Uvicorn | 47,533 | 34,263 | **2.5×** |
| aiohttp (later 1-worker, `production-peers.md`) | 51,264 | 34,887 | **~2.5×** |
| Litestar (later 1-worker) | 30,353 | 19,796 | **~4.3×** |
| Django-Bolt | 28,084 | 23,067 | **4.3×** |
| Robyn | 25,416 | 18,070 | **4.7×** |

Reads, params, and small uploads tell the same story: **about 2–2.5×**
the ASGI/uvloop frameworks people actually pick (FastAPI, Sanic,
BlackSheep), **~2×** Stario’s own Python protocol, **4×+** Robyn /
Django-Bolt.

That is “extremely faster than every Python framework that provides the
same HTTP surface,” as long as you do not put Granian or Socketify in
this table.

### Class A — native servers (1–1 is the deal)

| Stack | Plaintext | JSON | Params | Validate |
| --- | ---: | ---: | ---: | ---: |
| Socketify | 120,992 | 95,237 | 75,609 | 13,314 |
| **Stario Cython** | 120,378 | 117,556 | 104,519 | 82,402 |
| Granian RSGI (primary, noisy) | 86,164 ± 29k | 118,099 | 133,193 | 106,976 |
| Granian RSGI (later 1-worker run) | ~147k | ~136k | ~145k | ~108k |

- **Socketify:** tied on plaintext. We win JSON/params. They collapse on
  validate/forms (13k / 20k vs 82k / 94k). “1–1 on hello; we pull away
  when there is routing work and a body.”
- **Granian:** 1 worker can beat 1 Cython worker on reads (Rust accept).
  That is allowed. On 64KB we already tie (~30.8k). At **4 processes**
  (`STARIO_REUSE_PORT` vs `--workers`) Cython is **294k vs 224k**
  plaintext and **252k vs 144k** validate — we win the box.
- **Pyronova:** 1-worker they take plaintext **143k vs 129k**; we take
  validate **106k vs 87k**. `PYRONOVA_WORKERS>=2` crashes on this
  CPython 3.14 host. HttpArena 826k / their 429k are other boxes.
  Details: `httparena-pyronova.md`, `production-peers.md`.

BlackSheep + Granian (112k plaintext) is **not** a Class B counterexample.
Take Granian away (BlackSheep + Uvicorn = 59k) and it falls into the 2×
bucket. The 112k is Granian.

## Why Class B is slower (rationale)

1. **ASGI is a second HTTP stack.** FastAPI / BlackSheep / Uvicorn parse
   and dispatch in Python, then run the app. Stario’s protocol *is* the
   server (httptools or llhttp). No ASGI hop.
2. **Framework tax.** FastAPI’s bench still uses Starlette `Route` +
   per-request `ujson` (no Pydantic on most paths, no static
   `Response`). Sanic/BlackSheep still build framework request objects.
   We still lose ~2×. The gap is the stack, not “they validate and we
   don’t.”
3. **Cython is the protocol, not a different app.** Same
   `apps/stario_app.py` as Stario Python. ~1.9× is llhttp + less Python
   on the wire.
4. **Honest handlers.** No cached `Response`, no pre-serialized JSON.
   If we cheated, Class B would cry foul and the claim dies.

## Feature surface — no bench blindspots

The suite already forces the HTTP work a framework must do:

| Surface | In the suite | Stario |
| --- | --- | --- |
| Method + path routing | `/user/42` | trie, `{id}`, `{path...}`, host routes |
| JSON in/out | `/json`, `/validate`, `/echo/json` | `responses.json` + handler parse |
| Validation | `/validate` | app-level (same helper as peers) |
| Form body | `POST /form` | `await req.body()` |
| Buffered upload | 64KB / 2MB | `req.body()` |
| Streamed upload | `/ingest/stream/2m` | `async for req.stream()` |
| Multipart | `/upload` | raw body (same as most bench apps) |

Robyn and Django-Bolt **cannot stream** (documented). We can. That is
the opposite of a blindspot.

## What we do *not* claim (so the headline survives)

These are product choices, not secret bench skips. Say them once so
nobody “declassifies” *us*:

| Not in Stario | Who has it | Line |
| --- | --- | --- |
| WebSocket | FastAPI, Sanic, Socketify, Robyn | HTTP + SSE/Datastar is the product; Upgrade is 400 |
| OpenAPI / Pydantic | FastAPI, BlackSheep | Hypermedia stack, not a schema-first API framework |
| Parsed `UploadFile` / `Form()` | FastAPI, Sanic, BlackSheep | Raw body + stream; parse in the app if you need parts |
| ASGI mount / Uvicorn | the Class B field | We *are* the server |
| HTTP/2 | proxies | HTTP/1.1 + keepalive; put h2 on the edge |

If the audience is “JSON API with Swagger and WebSockets,” do not pick
this fight. If the audience is “Python HTTP server + routing +
HTML/SSE you can actually ship,” Class B is the field and the numbers
hold.

## Lines that are safe

**Headline:** Stario (Cython protocol) is the fastest Python HTTP
framework we have measured: **~2×** BlackSheep/Uvicorn and Stario
Python, **~2.3×** Sanic, **~2.5×** FastAPI, **~4×** Robyn and
Django-Bolt, on the same routes and honest handlers.

**Class A:** Native servers without a Python HTTP engine (Granian,
Socketify, **Pyronova**) match or beat us on hello-world. We stay
with Granian/Socketify on JSON/params and move ahead on body work;
with several processes we out-scale Granian. Pyronova has no
same-machine row — treat it as the fast Robyn, not as FastAPI.

**Do not say:** “Faster than Granian.” Say “faster than every Python
framework; 1–1 with thin native servers; faster than Granian when you
fill the machine.”

**Do not say:** “Faster than Go.” Say “ahead of stdlib `net/http` on
one core and on the box; 1-core fasthttp is a different class.”

**Do not say:** “Faster than Pyronova.” Say “1–1 on hello (they edge
plaintext); we win validate; they crash at 2+ TPC threads here.”

## HttpArena / aiohttp

[HttpArena](https://www.http-arena.com/) is the public table now that
TechEmpower is gone. **aiohttp is #1 Python on the HTTP/1.1 composite**
(240, gold within language). That is a complete standard-mode suite
on a 64-core box, not a plaintext RPS crown — tuned Sanic already
beats aiohttp on baseline/JSON and then loses the composite by
skipping TLS/static/DB.

aiohttp is **Class B**. On this machine, production setup, we are
**~2.5×** (1 worker) and **~1.8×** (×4) on plaintext
(`production-peers.md`). That is not an HttpArena rank. Full
write-up: `httparena-aiohttp.md`.

## HttpArena / Litestar

[Litestar](https://github.com/litestar-org/litestar) is Class B
(uvicorn + msgspec). HttpArena composite 236, just behind aiohttp.
On this machine it is the slowest of the three peers: **~4.3×**
behind 1-worker Cython, **~2.8×** behind ×4. Write-up:
`httparena-litestar.md`.

## HttpArena / Pyronova

[Pyronova](https://github.com/leocaolab/pyronova) is Hyper + Tokio
with PEP 684 sub-interpreters. HttpArena: **experimental + tuned**,
composite 431, baseline **826k** (~1.4× aiohttp). JSON 723k is a
per-worker byte cache; `/pipeline` is a Rust fast-path. Self-published
429k GET `/` is 16 workers on 8C/16T. Class A, not the 2× table.
1-worker here: they edge plaintext (143k vs 129k); we win validate;
2+ TPC threads crash. Full write-up: `httparena-pyronova.md`.
