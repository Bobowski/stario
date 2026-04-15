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

## Quick start

### From a template

`uvx` runs the Stario CLI without a global install. Everything after this is interactive (project name, template, and optional dev server).

```bash
uvx stario init
```

### Manual setup

```bash
uv init my-app   # creates a new uv project (pyproject, layout)
cd my-app
uv add stario
```

Put this in `main.py`:

```python
import stario.responses as responses
from stario import App, Context, Span, Writer


async def home(c: Context, w: Writer) -> None:
    responses.text(w, "Hello from Stario")


async def bootstrap(app: App, span: Span) -> None:
    span.attr("app.name", "example")
    app.get("/", home, name="home")
```

```bash
uv run stario watch main:bootstrap
```

Install with `pip install stario` if you are not using uv. During startup, `bootstrap` runs once: register routes there and attach attributes to `span` (telemetry for the lifecycle). Use `stario watch` in development so the process reloads when files change; use `stario serve` for a normal long-running server without reload. See [Getting started](https://stario.dev/docs) for project layout and CLI options. For containers, TLS, and production-oriented setup, see [Deployment: Containers, TLS, and safe releases](https://stario.dev/docs/how-tos/deployment-containers-and-tls).

## What you get

- Explicit wiring: `bootstrap(app, span)`, named routes, no hidden registration.
- Sharp primitives: `Context` for the request, `Writer` for the response, HTML via `stario.html`, telemetry via `span`.
- Hypermedia by default: HTML and SSE are first-class; realtime layers are optional when the product needs them.
- Observable runs: spans for startup and requests are part of how you structure apps, not an afterthought.

## What Stario is not

No bundled ORM, admin UI, or plugin discovery system. Databases, auth, and brokers stay in your code or thin adapters; the framework stays a focused HTTP and hypermedia core.

## Releases

Version history and upgrade notes live in [`CHANGELOG.md`](CHANGELOG.md).
