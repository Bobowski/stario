"""
Stario Hello World.

Minimal starter with Datastar.
Run with: uv run main.py
"""

import asyncio
from dataclasses import dataclass
from pathlib import Path

from stario import Context, RichTracer, Stario, Writer, asset, at, data
from stario.html import H1, Body, Button, Div, Head, Html, Meta, P, Script, Title
from stario.toys import toy_inspector

# =============================================================================
# Views
# =============================================================================


def page(*children):
    """Base HTML page with Datastar."""
    return Html(
        {"lang": "en"},
        Head(
            Meta({"charset": "UTF-8"}),
            Meta(
                {"name": "viewport", "content": "width=device-width, initial-scale=1"}
            ),
            Title("Hello World - Stario App"),
            Script({"type": "module", "src": "/static/" + asset("js/datastar.js")}),
        ),
        Body(
            {
                "style": "font-family: system-ui; padding: 2rem; max-width: 600px; margin: 0 auto;"
            },
            *children,
        ),
    )


def home_view(count: int):
    """Home page with a counter example."""
    return page(
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
                    data.on("click", at.get("/increment")),
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
    w.html(home_view(count=0))


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

    with RichTracer() as tracer:
        app = Stario(tracer)

        # Static files with fingerprinting
        app.assets("/static", Path(__file__).parent / "static")

        # Routes
        app.get("/", home)
        app.get("/increment", increment)

        # Start server
        await app.serve(host="127.0.0.1", port=8000)


if __name__ == "__main__":
    asyncio.run(main())
