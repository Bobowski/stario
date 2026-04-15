"""
Stario Hello World.

Minimal starter with Datastar (client signals + server-driven sync). For a longer
walkthrough of HTML-as-call-trees, SSE, and in-process ``Relay`` fan-out, see the
``tiles`` project template from ``stario init``.

Run with: uv run stario watch main:bootstrap
      or: uv run stario serve main:bootstrap
"""

from pathlib import Path

# Core: Context (request + span), Writer (transport), App (routes + assets).
# `stario.html` holds tag constructors; imported as `h` so names like telemetry `Span` stay unambiguous.
from stario import App, Context, Span, StaticAssets, Writer
from stario import datastar as ds
from stario import html as h
from stario import responses as responses
from stario.toys import toy_inspector

# =============================================================================
# Views
# =============================================================================
#
# Pure functions build trees of `stario.html` callables (`h.Div`, `h.Button`, …).
# `responses.html()` and `ds.sse.patch_signals()` send them to the client;
# string children are escaped.


def page(app: App, *children):
    """Base HTML page with Datastar served from fingerprinted assets."""
    return h.HtmlDocument(
        {"lang": "en"},
        h.Head(
            h.Meta({"charset": "UTF-8"}),
            h.Meta(
                {"name": "viewport", "content": "width=device-width, initial-scale=1"}
            ),
            h.Title("Hello World - Stario App"),
            h.Script({"type": "module", "src": app.url_for("static:js/datastar.js")}),
        ),
        h.Body(
            {
                "style": "font-family: system-ui; padding: 2rem; max-width: 600px; margin: 0 auto;"
            },
            *children,
        ),
    )


def home_view(count: int, app: App):
    """Home page with a counter example."""
    return page(
        app,
        toy_inspector(),
        h.Div(
            # Signals: client-held JSON; Datastar sends them on each @get / @post to this app.
            ds.signals({"count": count}),
            h.H1("Hello, Stario! ⭐"),
            h.P(
                {"style": "color: #666; margin-bottom: 1.5rem;"},
                "A minimal starter with Datastar reactivity.",
            ),
            h.Div(
                {"style": "display: flex; align-items: center; gap: 1rem;"},
                h.Button(
                    {
                        "style": "padding: 0.5rem 1rem; font-size: 1.25rem; cursor: pointer;",
                    },
                    ds.on("click", "$count--"),
                    "-",
                ),
                h.Div(
                    {
                        "id": "count",
                        "style": (
                            "font-size: 2rem; font-weight: bold; min-width: 3rem; "
                            "text-align: center;"
                        ),
                    },
                    ds.text("$count"),
                ),
                h.Button(
                    {
                        "style": "padding: 0.5rem 1rem; font-size: 1.25rem; cursor: pointer;",
                    },
                    ds.on("click", "$count++"),
                    "+",
                ),
            ),
            h.P(
                {"style": "margin-top: 2rem; color: #666;"},
                "Or fetch from server: ",
                h.Button(
                    {"style": "padding: 0.25rem 0.5rem; cursor: pointer;"},
                    ds.on("click", ds.get(app.url_for("increment"))),
                    "Server +1",
                ),
            ),
        ),
    )


# =============================================================================
# Handlers
# =============================================================================
#
# async def handler(c: Context, w: Writer) — same contract as larger Stario apps.


async def home(c: Context, w: Writer) -> None:
    """Serve the home page."""
    responses.html(w, home_view(count=0, app=c.app))


async def increment(c: Context, w: Writer) -> None:
    """Bump counter from the server and push the new signals to the client (SSE)."""
    signals = await ds.read_signals(c.req)
    raw = signals.get("count", 0)
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = 0
    signals["count"] = n + 1
    # SSE signal patches update client state without replacing the whole page.
    ds.sse.patch_signals(w, signals)


# =============================================================================
# Bootstrap
# =============================================================================


async def bootstrap(app: App, span: Span) -> None:
    static_dir = Path(__file__).parent / "static"
    static_dir_display = (
        static_dir.relative_to(Path.cwd())
        if static_dir.is_relative_to(Path.cwd())
        else static_dir
    )
    span.attr("static_dir", str(static_dir_display))
    app.mount("/static", StaticAssets(static_dir, name="static"))

    app.get("/", home, name="home")
    app.get("/increment", increment, name="increment")
