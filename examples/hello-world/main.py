"""
Stario Hello World.

Minimal starter with Datastar (client signals + server-driven sync). For a longer
walkthrough of HTML-as-call-trees, SSE, and in-process `Relay` fan-out, see
`examples/tiles` in the stario repo.

Run with: uv run stario watch main:bootstrap
      or: uv run stario serve main:bootstrap
"""

from pathlib import Path

import stario.responses as responses
from stario import App, AssetManifest, Context, Span, StaticAssets, UrlPath, Writer
from stario.datastar import SSE, at, data, read_signals
from stario.markup import html as h

# Core: Context (request + span), Writer (transport), App (routes + assets).
# `stario.markup.html` holds tag constructors; imported as `h` so names like telemetry `Span` stay unambiguous.

# Cheap at import time: scan + fingerprint only. Serving (compression, caching)
# happens in bootstrap via StaticAssets.
ASSETS = AssetManifest(Path(__file__).parent / "static")

HOME = UrlPath("/")
INCREMENT = UrlPath("/increment")

# =============================================================================
# Views
# =============================================================================
#
# Pure functions build trees of `stario.markup.html` callables (`h.Div`, `h.Button`, …).
# `responses.html()` and `SSE(w).patch_signals()` send them to the client;
# string children are escaped.


def page(*children):
    """Base HTML page with Datastar served from fingerprinted assets."""
    return h.HtmlDocument(
        {"lang": "en"},
        h.Head(
            h.Meta({"charset": "UTF-8"}),
            h.Meta(
                {"name": "viewport", "content": "width=device-width, initial-scale=1"}
            ),
            h.Title("Hello World - Stario App"),
            h.Script({"type": "module", "src": ASSETS.href("js/datastar.js")}),
        ),
        h.Body(
            {
                "style": "font-family: system-ui; padding: 2rem; max-width: 600px; margin: 0 auto;"
            },
            *children,
        ),
    )


def home_view(count: int):
    """Home page with a counter example."""
    return page(
        h.Div(
            # Signals: client-held JSON; Datastar sends them on each @get / @post to this app.
            data.signals({"count": count}),
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
                    data.on("click", "$count--"),
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
                    data.text("$count"),
                ),
                h.Button(
                    {
                        "style": "padding: 0.5rem 1rem; font-size: 1.25rem; cursor: pointer;",
                    },
                    data.on("click", "$count++"),
                    "+",
                ),
            ),
            h.P(
                {"style": "margin-top: 2rem; color: #666;"},
                "Or fetch from server: ",
                h.Button(
                    {"style": "padding: 0.25rem 0.5rem; cursor: pointer;"},
                    data.on("click", at.get(INCREMENT.href())),
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
    responses.html(w, home_view(count=0))


async def increment(c: Context, w: Writer) -> None:
    """Bump counter from the server and push the new signals to the client (SSE)."""
    signals = await read_signals(c.req)
    raw = signals.get("count", 0)
    try:
        n = int(raw)
    except TypeError, ValueError:
        n = 0
    signals["count"] = n + 1
    # SSE signal patches update client state without replacing the whole page.
    SSE(w).patch_signals(signals)


# =============================================================================
# Bootstrap
# =============================================================================


async def bootstrap(app: App, span: Span):
    static_dir_display = (
        ASSETS.directory.relative_to(Path.cwd())
        if ASSETS.directory.is_relative_to(Path.cwd())
        else ASSETS.directory
    )
    span.attr("static_dir", str(static_dir_display))
    with span.step("static_assets") as s:
        static = StaticAssets(ASSETS)
        s.attrs(static.stats)
    static.register(app)

    app.get(HOME, home)
    app.get(INCREMENT, increment)
    yield
