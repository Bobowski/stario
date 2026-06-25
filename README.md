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

Stario is a small Python framework for enjoyable realtime hypermedia apps. It helps you build web apps where HTTP, HTML, and streaming stay visible in your code. Handlers are plain async functions; routes are registered explicitly; responses go through a dedicated writer. When the UI needs live updates, you can add [Datastar](https://stario.dev/docs/reference/datastar) and [Relay](https://stario.dev/docs/reference/toolbox#relay) without throwing away the same request/response mental model. The [realtime tiles](https://stario.dev/docs/tutorials/realtime-tiles) tutorial walks through the full pattern end to end.

Full guides, API reference, and tutorials live at [stario.dev](https://stario.dev). This page is a short orientation for people landing on the repository.

## Where Stario fits

Stario is an asyncio-native HTTP stack: you write async handlers and register routes on an `App`, and the `stario` CLI runs a built-in HTTP server (TCP or a Unix domain socket). It is not an ASGI application you mount in Uvicorn or Hypercorn; wiring goes through the `bootstrap` hook, `Context`, and `Writer` instead.

## Requirements

Python 3.14 or newer is required. The package tracks current Python and the standard library (including APIs the framework builds on) rather than supporting older runtimes.

**uvloop (optional):** Stario defaults to the stdlib asyncio loop. For a faster event loop on Linux/macOS, install the optional extra and set `STARIO_LOOP=uvloop`:

```bash
uv add "stario[uvloop]"
# or: pip install "stario[uvloop]"
```

Then run with `STARIO_LOOP=uvloop stario serve main:bootstrap` (or `stario watch`). uvloop is not supported on Windows.

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
from stario import App, Context, Span, UrlPath, Writer


async def home(c: Context, w: Writer) -> None:
    responses.text(w, "Hello from Stario")


HOME = UrlPath("/")

async def bootstrap(app: App, span: Span):
    span.attr("app.name", "example")
    app.get(HOME, home)
    yield
```

```bash
uv run stario watch main:bootstrap
```

Install with `pip install stario` if you are not using uv. During startup, `bootstrap` runs until its single `yield`: register routes and attach attributes to `span` before `yield`; put teardown after `yield` when needed. Use `stario watch` in development so the process reloads when files change; use `stario serve` for a normal long-running server without reload. Server runtime policy (`STARIO_HOST`, `STARIO_PORT`, `STARIO_TRACER`, and related vars) is configured through environment variables — see `stario serve --help` (Stario does not load `.env` files; export vars in your shell or use your own dotenv tooling). See [Getting started](https://stario.dev/docs) for project layout. For containers, TLS, and production-oriented setup, see [Deployment: Containers, TLS, and safe releases](https://stario.dev/docs/how-tos/deployment-containers-and-tls).

## What you get

- Explicit wiring: async-generator `bootstrap(app, span)` with a single `yield`, `UrlPath` constants, no hidden registration.
- Sharp primitives: `Context` for the request, `Writer` for the response, HTML/SVG trees via `stario.markup`, telemetry via `span`.
- Static assets: `AssetManifest` for fingerprinted URLs, `StaticAssets(manifest).register(app)` in bootstrap.
- Hypermedia by default: HTML and SSE are first-class; realtime layers are optional when the product needs them.
- Observable runs: spans for startup and requests are part of how you structure apps, not an afterthought.

## What Stario is not

No bundled ORM, admin UI, or plugin discovery system. Databases, auth, and brokers stay in your code or thin adapters; the framework stays a focused HTTP and hypermedia core.

## Releases

Version history and upgrade notes live in [`CHANGELOG.md`](CHANGELOG.md).

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
