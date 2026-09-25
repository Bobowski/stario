<p align="center">
  <picture>
    <img alt="Stario" src="https://raw.githubusercontent.com/bobowski/stario/main/docs/img/stario.png" style="height: 160px; width: auto;">
  </picture>
</p>

<p align="center">
  <strong>Stario</strong><br>
  Craft realtime hypermedia apps that are a joy to write and ship.
</p>

<p align="center">
  <a href="https://stario.dev">Documentation</a>
  ·
  <a href="https://github.com/bobowski/stario">Source</a>
</p>

---

Stario is a small Python framework for enjoyable realtime hypermedia apps. It helps you build web apps where HTTP, HTML, and streaming stay visible in your code. Handlers are plain async functions; routes are registered explicitly; responses go through a dedicated writer. When the UI needs live updates, you can add [Datastar](https://stario.dev/docs/how-tos/datastar-sdk) and [Relay](https://stario.dev/docs/reference/toolbox#relay) without throwing away the same request/response mental model. The idea is [Go-to architecture](https://stario.dev/docs/explanation/go-to-architecture). The [SDK](https://stario.dev/docs/how-tos/datastar-sdk) and [tiles tutorial](https://stario.dev/docs/tutorials/realtime-tiles) live here.

Full guides, API reference, and tutorials live at [stario.dev](https://stario.dev). This page is a short orientation for people landing on the repository.

## Where Stario fits

Stario is an asyncio-native HTTP stack: you write async handlers and register routes on an `App`, and you start the built-in HTTP server (TCP or a Unix domain socket) with `asyncio.run(stario.serve(bootstrap))` (or `uvloop.run(...)`) or the `stario` CLI. It is not an ASGI application you mount in Uvicorn or Hypercorn; wiring goes through the `bootstrap` hook, `Context`, and `Writer` instead.

## Requirements

Python 3.12 or newer is required.

**uvloop (optional):** Stario defaults to the stdlib asyncio loop. For a faster event loop on Linux/macOS, install the optional extra and set `STARIO_LOOP=uvloop`:

```bash
uv add "stario[uvloop]"
# or: pip install "stario[uvloop]"
```

Then run with `STARIO_LOOP=uvloop stario serve main:bootstrap` (or `stario watch`). uvloop is not supported on Windows.

### JSON codec

Stario uses one process-wide JSON codec for responses, Datastar signals,
telemetry, and the test client. The default codec uses the standard library and
emits compact UTF-8 JSON. Replace it explicitly when the application uses
another library:

```python
import msgspec

import stario.json as stario_json


class MsgspecCodec:
    def dumps(self, value, *, default=None):
        return self.dumps_bytes(value, default=default).decode()

    def dumps_bytes(self, value, *, default=None):
        return msgspec.json.encode(value, enc_hook=default)

    def loads(self, data):
        return msgspec.json.decode(data)


stario_json.set_codec(MsgspecCodec())
```

orjson has native byte output, so its byte path does not encode text first:

```python
import orjson

import stario.json as stario_json


class OrjsonCodec:
    def dumps(self, value, *, default=None):
        return self.dumps_bytes(value, default=default).decode()

    def dumps_bytes(self, value, *, default=None):
        return orjson.dumps(value, default=default)

    def loads(self, data):
        return orjson.loads(data)


stario_json.set_codec(OrjsonCodec())
```

`dumps()` returns text, `dumps_bytes()` returns UTF-8 bytes, and `loads()`
accepts text, bytes, or a byte array. Stario uses bytes for HTTP and SSE and
text for HTML attributes and telemetry storage. A byte-native codec only
decodes when a text consumer asks for `dumps()`.

Calling `set_codec()` again replaces the codec for later operations. Stario
does not synchronize replacement with active requests or telemetry writes.
Configure during application setup unless changing live serialization is
intentional. The `default` callback is backend-dependent: a codec may serialize
its native datetime, UUID, Decimal, or model types before calling it.

This is transport configuration only; validation and application models stay
in application code.

## Quick start

### From an example

Clone the repo (or copy an example directory) and run:

```bash
git clone https://github.com/bobowski/stario.git
cd stario/examples/tiles
uv sync
uv run stario watch main:bootstrap
```

See [`examples/`](examples/) for **tiles** (recommended), **hello-world**, and **chat-room** (multi-file layout).

### Manual setup

```bash
uv init my-app   # creates a new uv project (pyproject, layout)
cd my-app
uv add stario
```

Put this in `main.py`:

```python
import stario.responses as responses
from stario import App, Context, Route, Span, Writer


async def home(c: Context, w: Writer) -> None:
    responses.text(w, "Hello from Stario")


HOME = Route("GET", "/")


async def bootstrap(app: App, span: Span):
    span.attr("app.name", "example")
    app.add(HOME, home)
    yield
```

```bash
uv run stario watch main:bootstrap
```

To start the same app from Python, await `serve` on a loop you start
(and continue after shutdown):

```python
import asyncio
from stario import serve

if __name__ == "__main__":
    asyncio.run(serve(bootstrap))
```

Use `uvloop.run(serve(bootstrap))` when you want uvloop. Pass listen
settings as keywords: `serve(bootstrap, host="0.0.0.0", port=9000)`.
`Server` takes a `ServerConfig` object.

Install with `pip install stario` if you are not using uv. During startup, `bootstrap` runs until its single `yield`: register routes and attach attributes to `span` before `yield`; put teardown after `yield` when needed. Use `stario watch` in development so the process reloads when files change; use `stario serve` for a normal long-running server without reload. Server runtime policy (`STARIO_HOST`, `STARIO_PORT`, `STARIO_TRACER`, and related vars) is configured through environment variables — see `stario serve --help` (Stario does not load `.env` files; export vars in your shell or use your own dotenv tooling). See [Getting started](https://stario.dev/docs) for project layout. For containers, TLS, and production-oriented setup, see [Deployment, containers, and TLS](https://stario.dev/docs/how-tos/deployment-containers-and-tls).

### Filesystem URLs

Build `Assets` or `Files` at module level. Call `href()` there. Call
`await attach(app)` in bootstrap (register + load). `Assets` precompresses
by default; `Files` does not unless you pass `precompress=`:

```python
from stario import App, Assets, Files, Span

ASSETS = Assets("./static", "/static")
UPLOADS = Files("./uploads", "/data")
STYLE_CSS = ASSETS.href("css/style.css")


async def bootstrap(app: App, span: Span):
    span.attrs(await ASSETS.attach(app))
    await UPLOADS.attach(app, precompress=("br", "gzip"))
    yield
```

`Assets` hashes names and 307s the logical path. Both send strong ETags
and `X-Content-Type-Options: nosniff`. `stario.staticassets` is obsolete.

## What you get

- Explicit wiring: async-generator `bootstrap(app, span)` with a single `yield`, `Route` endpoints, no hidden registration.
- Sharp primitives: `Context` for the request, `Writer` for the response, HTML/SVG trees via `stario.markup`, telemetry via `span`.
- Files: `Assets` and `Files` expose a directory at a URL prefix.
  `attach(app)` registers GET/HEAD and loads the tree. `Assets`
  hashes names and 307s the logical path. Both use strong ETags and 304.
  Import from `stario` or `stario.filesystem`. `stario.staticassets` is
  obsolete.
- Hypermedia by default: HTML and SSE are first-class; realtime layers are optional when the product needs them.
- Observable runs: spans for startup and requests are part of how you structure apps, not an afterthought.

## What Stario is not

No bundled ORM, admin UI, or plugin discovery system. Databases, auth, and brokers stay in your code or thin adapters; the framework stays a focused HTTP and hypermedia core.

## Releases

Version history and upgrade notes live in [`CHANGELOG.md`](CHANGELOG.md).
A tag `v*` on `main` builds the sdist and wheel and uploads them to PyPI.

## Contributing

From `stario/`:

```bash
uv sync
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
```

Before committing:

```bash
uv run ruff check . --fix
uv run ruff format .
```
