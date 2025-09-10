<p align="center">
  <picture>
    <img alt="stario-logo" src="https://raw.githubusercontent.com/bobowski/stario/main/docs/img/stario.png">
  </picture>
</p>

<p align="center">
  <em>✨ Binging back joy and exploration to building web apps ✨</em>
</p>

---

**Documentation**: Coming soon

**Source Code**: <a href="https://github.com/bobowski/stario/tree/main/stario" target="_blank">https://github.com/bobowski/stario</a>

---

### Stario

Stario is a lightweight ASGI framework built on top of [Starlette](https://www.starlette.io/)
that brings back the joy of building web apps.
Focus on building apps rather than passing data between components.

### Why Stario?

- HTML-first - dropping intermediary data layers (e.g. JSON) and using HTML as the primary data format. "Just throw html at user and make them happy."
- Rapid prototyping - a simple framework to build and test fullstack ideas with minimal setup. Build, see if it works, optimize later (if ever).
- No-nonsense - no ceremony, no complexity, just build.
- Learning experience - Learn about web, rather than X or Y language framework. I've learned a lot building this framework. I hope you will too.

It focuses on:

- **HTML-first DX**: return strings or HTML providers, no ceremony.
- **Realtime by default**: seamless Server-Sent Events (SSE) with [DataStar](https://data-star.dev/) patches/signals.
- **Great defaults**: Brotli compression out of the box, fast dependency resolution, and caching.
- **Starlette compatibility**: interop with routes, middleware, and tools you already know.

What you get:

- **A tiny, low-complexity HTTP framework** powered by [Starlette](https://www.starlette.io/) and [DataStar](https://data-star.dev/).
- **SSE streaming** by simply yielding patches, HTML, or signals (dicts).
- **Brotli compression** middleware enabled by default (GZip fallback).
- **Simple DI** via `Annotated` with request params (`QueryParam`, `PathParam`, `Header`, `Cookie`) and `Inject`.
- **Per-request/app/ttl caching** and configurable run modes (`auto`, `sync`, `thread`).
- **Header-constrained routes** and a lightweight `StarRouter`.
- **Few dependencies** and a clear, typed codebase.

---

## Installation

From PyPI (when available):

```shell
pip install stario
```

Or from source in this repository:

```shell
cd stario
pip install -e .
```

You'll also want an ASGI server such as [uvicorn](https://www.uvicorn.org/):

```shell
pip install uvicorn
```

## Quick start

```python
# main.py
import asyncio
from stario import Stario, Query, Command
from stario.toys import ToyPage


async def home():
    return ToyPage(
        """
        <h2>Realtime responses!</h2>
        <div data-on-load="@get('/online-counter')">
            This shows how long the connection has been open.
        </div>
        <div id="online-counter"></div>
        """
    )


async def online_counter():
    duration = 0
    interval = 0.01
    while True:
        # Yielding strings/patches streams as SSE with DataStar-compatible events
        yield f"<div id='online-counter'>Online since: {duration:.1f}s</div>"
        duration += interval
        await asyncio.sleep(interval)


# Building the app
app = Stario(
    Query("/", home),
    Query("/online-counter", online_counter),
)
```

Run with Uvicorn:

```shell
uvicorn main:app --reload
```

Open `http://127.0.0.1:8000/` in your browser.

---

<p align="center"><i>Stario code is designed & crafted for joy.</i><br/>&mdash; ⭐️ &mdash;</p>
