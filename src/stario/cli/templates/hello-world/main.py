"""
Stario Hello World.

Minimal starter with Datastar.
Run with: uv run stario watch main:bootstrap
      or: uv run stario serve main:bootstrap
"""

import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from stario import Context, JsonTracer, RichTracer, Stario, UrlFor, Writer, at, data
from stario import Span as TraceSpan
from stario.html import H1, Body, Button, Div, Head, Html, Meta, P, Script, Title
from stario.http.server import Server
from stario.toys import toy_inspector

# =============================================================================
# Views
# =============================================================================


def page(url_for: UrlFor, *children):
    """Base HTML page with Datastar served from fingerprinted assets."""
    return Html(
        {"lang": "en"},
        Head(
            Meta({"charset": "UTF-8"}),
            Meta(
                {"name": "viewport", "content": "width=device-width, initial-scale=1"}
            ),
            Title("Hello World - Stario App"),
            Script({"type": "module", "src": url_for("static", "js/datastar.js")}),
        ),
        Body(
            {
                "style": "font-family: system-ui; padding: 2rem; max-width: 600px; margin: 0 auto;"
            },
            *children,
        ),
    )


def home_view(count: int, url_for: UrlFor):
    """Home page with a counter example."""
    return page(
        url_for,
        toy_inspector(),
        Div(
            # Signals: client-side reactive state
            data.signals({"count": count}),
            H1("Hello, Stario! ⭐"),
            P(
                {"style": "color: #666; margin-bottom: 1.5rem;"},
                "A minimal starter with Datastar reactivity.",
            ),
            # Counter example
            Div(
                {"style": "display: flex; align-items: center; gap: 1rem;"},
                Button(
                    {
                        "style": "padding: 0.5rem 1rem; font-size: 1.25rem; cursor: pointer;",
                    },
                    # data.on() creates data-on-click for Datastar
                    # $count is a reactive signal
                    data.on("click", "$count--"),
                    "-",
                ),
                Div(
                    {
                        "id": "count",
                        "style": "font-size: 2rem; font-weight: bold; min-width: 3rem; text-align: center;",
                    },
                    data.text("$count"),
                ),
                Button(
                    {
                        "style": "padding: 0.5rem 1rem; font-size: 1.25rem; cursor: pointer;",
                    },
                    data.on("click", "$count++"),
                    "+",
                ),
            ),
            # Server interaction example
            P(
                {"style": "margin-top: 2rem; color: #666;"},
                "Or fetch from server: ",
                Button(
                    {"style": "padding: 0.25rem 0.5rem; cursor: pointer;"},
                    data.on("click", at.get(url_for("increment"))),
                    "Server +1",
                ),
            ),
        ),
    )


# =============================================================================
# Handlers
# =============================================================================


async def home(c: Context, w: Writer) -> None:
    """Serve the home page."""
    w.html(home_view(count=0, url_for=c.url_for))


@dataclass
class HomeSignals:
    count: int = 0


async def increment(c: Context, w: Writer) -> None:
    """Increment the counter via SSE patch."""
    signals = await c.signals(HomeSignals)
    signals.count += 1
    w.sync(signals)


# =============================================================================
# App
# =============================================================================


async def main():
    tracer_factory = RichTracer if sys.stdout.isatty() else JsonTracer

    with tracer_factory() as tracer:
        server = Server(bootstrap, tracer)
        await server.run()


@asynccontextmanager
async def bootstrap(app: Stario, span: TraceSpan) -> AsyncIterator[None]:
    static_dir = Path(__file__).parent / "static"
    static_dir_display = (
        static_dir.relative_to(Path.cwd()) if static_dir.is_relative_to(Path.cwd()) else static_dir
    )
    span.attr("static_dir", str(static_dir_display))
    app.assets("/static", static_dir, name="static")

    # Routes
    app.get("/", home, name="home")
    app.get("/increment", increment, name="increment")

    yield


if __name__ == "__main__":
    asyncio.run(main())
