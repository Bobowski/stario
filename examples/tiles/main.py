"""
Stario Tiles — your first realtime hypermedia app (single file).

Collaborative painting board: HTTP for the page and commands, SSE for live
updates, in-process Relay to fan out between open tabs.

Run with: uv run stario watch main:bootstrap
      or: uv run stario serve main:bootstrap

Read top to bottom:

  1. URLs and assets — UrlPath routes, fingerprinted static files
  2. State           — Game (board + presence); built in bootstrap
  3. Views           — pure HTML from Game
  4. Handlers        — queries (GET) and commands (POST)
  5. Bootstrap       — wire routes, annotate span

Three routes, three jobs (CQRS-shaped, not a framework):

  GET  /           first paint — build HTML once
  GET  /subscribe  stay open — patch HTML whenever Relay fires
  POST /click      change board — 204 immediately, updates arrive on SSE

Handlers use c.span for request telemetry; bootstrap uses span.attrs at startup.
For a multi-file layout, clone examples/chat-room from the stario repo
(https://github.com/bobowski/stario/tree/main/examples/chat-room).
"""

import random
import uuid
from pathlib import Path

import stario.responses as responses
from stario import (
    App,
    AssetManifest,
    Context,
    Relay,
    Span,
    StaticAssets,
    UrlPath,
    Writer,
)
from stario.datastar import SSE, at, data, read_signals
from stario.markup import HtmlElement, baked, classes, styles
from stario.markup import html as h

# Stario apps center on: Context (per-request), Writer (transport), and plain Python
# instead of a template language. HTML tags live under `stario.markup.html`;
# importing that catalog as `h` keeps tag names (`h.Div`, `h.Span`) separate from framework
# symbols like telemetry `Span` (same name as <span>, different concept).

# Configuration
# For a larger layout (handlers vs views, shared state, SQLite), see
# examples/chat-room in the stario repo.
# Views
# Handlers
# =============================================================================
# 1. URLs and assets
# =============================================================================


# Cheap at import time: scan + fingerprint only. Serving (compression, caching)
# is paid in bootstrap when StaticAssets wraps the manifest.
ASSETS = AssetManifest(Path(__file__).parent / "static")
# Fingerprinted once at import — links stay stable across deploys that hash files.
STYLE_CSS = ASSETS.href("css/style.css")
DATASTAR_JS = ASSETS.href("js/datastar.js")

# One constant per route — used in app.get/post and in views that build URLs.
HOME = UrlPath("/")
SUBSCRIBE = UrlPath("/subscribe")
CLICK = UrlPath("/click")

# =============================================================================
# 2. State
# =============================================================================
# State
# In production, you'd use a database. Here, module-level dict/set keep
# everything in one file so the flow is easy to read from top to bottom.
# Views are pure functions that build HTML using the callables in
# `stario.markup.html` (each tag is a function-like object: Div, Span, …). You nest
# them like a normal function call tree; the framework turns that tree into
# markup when you pass it to `responses.html()` or
# `SSE(w).patch_elements()`, with escaping applied
# to ordinary strings so user-controlled text is safe by default.
# This file imports `stario.markup.html` as `h` so tag constructors read as `h.Div`,
# `h.Span`, etc., and do not collide with unrelated names such as telemetry
# `Span` from `stario` (the tracing handle, not the HTML element).
#
# Game is the source of truth. Commands mutate it; queries read it and pass it
# into views. Handler factories close over one Game instance from bootstrap.


class Game:
    """In-memory board and presence for this demo process."""

    COLORS = (
        "#ef4444",  # red
        "#f97316",  # orange
        "#eab308",  # yellow
        "#22c55e",  # green
        "#14b8a6",  # teal
        "#3b82f6",  # blue
        "#8b5cf6",  # violet
        "#ec4899",  # pink
        "#6366f1",  # indigo
        "#06b6d4",  # cyan
    )

    def __init__(self, *, grid_size: int = 5, colors: tuple[str, ...] | None = None):
        palette = colors or self.COLORS
        self.grid_size = grid_size
        self.colors = palette
        self.board: dict[int, str] = {}  # cell index → paint color
        self.user_colors: dict[str, str] = {}  # only tabs with an open /subscribe

        total = grid_size * grid_size
        # Partial fill so first paint looks lived-in, not an empty grid.
        for cell in random.sample(range(total), int(total * 0.6)):
            self.board[cell] = random.choice(palette)

    @property
    def total_cells(self) -> int:
        return self.grid_size * self.grid_size

    def join(self, user_id: str) -> None:
        # Stable color per id — reconnecting the same tab keeps the same swatch.
        if user_id not in self.user_colors:
            self.user_colors[user_id] = random.Random(user_id).choice(self.colors)

    def leave(self, user_id: str) -> None:
        # SSE closed — drop from roster so other tabs stop counting this player.
        self.user_colors.pop(user_id, None)

    def paint_cell(self, user_id: str, cell_id: int) -> str:
        """Toggle cell with this player's color. Returns painted or cleared."""
        color = self.user_colors[user_id]
        # Same color again erases — quick undo without a separate erase action.
        if self.board.get(cell_id) == color:
            self.board.pop(cell_id, None)
            return "cleared"
        self.board[cell_id] = color
        return "painted"


# =============================================================================
# 3. Views
# =============================================================================
#
# Pure functions: Game in, HTML out. Same home_view for the first GET and every
# SSE patch so the live DOM always matches server state. List-valued class
# skips None/False; None children are omitted too (conditional tokens and nodes).
#
# @baked freezes the document shell at import; body is the one dynamic slot.


@baked
def page(body: HtmlElement):
    """Document shell — static head, dynamic body."""
    return h.HtmlDocument(
        {"lang": "en"},
        h.Head(
            h.Meta({"charset": "UTF-8"}),
            h.Meta(
                {"name": "viewport", "content": "width=device-width, initial-scale=1"}
            ),
            h.Title("Tiles - Stario App"),
            h.Link({"rel": "stylesheet", "href": STYLE_CSS}),
            h.Script({"type": "module", "src": DATASTAR_JS}),
        ),
        h.Body(body),
    )


def cell_view(cell_id: int, color: str | None) -> HtmlElement:
    return h.Div(
        {
            "data-cell-id": str(cell_id),  # read by the board's data.on click handler
        },
        classes("cell", "painted" if color else None),
        styles({"background-color": color}) if color else None,
    )


def board_view(game: Game) -> HtmlElement:
    size = game.grid_size
    complete = len(game.board) == game.total_cells

    return h.Div(
        {"id": "board"},
        classes("board", "complete" if complete else None),
        data.on(
            "click",
            # Command goes to POST /click; Datastar attaches page signals automatically.
            f"""
            let id = evt.target.dataset.cellId;
            if (id) {{ @post(`{CLICK.href()}?cellId=${{id}}`); }}
            """,
        ),
        [
            h.Div(
                {"class": "row"},
                [
                    cell_view(row * size + col, game.board.get(row * size + col))
                    for col in range(size)
                ],
            )
            for row in range(size)
        ],
    )


def info_view(user_id: str, game: Game) -> HtmlElement:
    # Before /subscribe runs, this tab isn't in user_colors yet — show a neutral swatch.
    my_color = game.user_colors.get(user_id, "#ccc")
    return h.Div(
        {"id": "info", "class": "info-panel"},
        h.Div(
            {"class": "info-row"},
            h.Span({"class": "info-label"}, "Players"),
            h.Ul(
                {"id": "users", "class": "users-list"},
                [
                    h.Li(
                        h.Span(
                            {"class": "swatch"},
                            styles({"background-color": color}),
                        )
                    )
                    for color in game.user_colors.values()
                ]
                or [h.Li({"class": "empty"}, "...")],
            ),
        ),
        h.Div(
            {"class": "info-row"},
            h.Span({"class": "info-label"}, "Your color"),
            h.Span({"class": "swatch"}, styles({"background-color": my_color})),
        ),
    )


def home_view(user_id: str, game: Game) -> HtmlElement:
    return page(
        h.Div(
            {"id": "home", "class": "container"},  # patch target — id must stay stable
            # if_missing=True: reconnect must not overwrite client-held signal values.
            # Signals: client-held JSON synced with the server; every Datastar @get/@post includes them.
            # if_missing=True: set defaults only when absent so reconnects don't clobber client state.
            data.signals({"user_id": user_id}, if_missing=True),
            # Mount opens GET /subscribe; retry="always" survives brief network blips.
            data.init(at.get(SUBSCRIBE.href(), retry="always")),
            # data.init runs once when the node mounts — here it opens the SSE stream (`@get`).
            h.H1("Tiles - Stario App"),
            h.P(
                {"class": "subtitle"},
                "Click cells to paint. Everyone sees changes live!",
            ),
            info_view(user_id, game),
            board_view(game),
        ),
    )


# =============================================================================
# 4. Handlers
# =============================================================================
#
# Each route is a factory — home(game) returns the real handler so bootstrap can
# inject shared state without globals. hello-world skips this; tiles needs it.
#
# Queries (read):  home, subscribe
# Commands (write): click


async def read_user_id(c: Context) -> str:
    # Datastar attaches page signals to its GET and POST requests.
    # Reads Datastar signals from the client and returns plain JSON-shaped data.
    signals = await read_signals(c.req)
    return str(signals.get("user_id", ""))


def home(game: Game):
    """GET / — mint a user_id and render the page."""

    async def handler(c: Context, w: Writer) -> None:
        # Fresh id per tab — becomes a Datastar signal for later POSTs and SSE.
        user_id = str(uuid.uuid4())[:8]
        c.span.attr("user_id", user_id)
        responses.html(w, home_view(user_id, game))

    return handler


def subscribe(game: Game, relay: Relay[str]):
    """GET /subscribe — SSE stream; re-render home_view on every relay event."""

    async def handler(c: Context, w: Writer) -> None:
        user_id = await read_user_id(c)
        if not user_id:
            c.span.event("Missing user id", {})
            responses.redirect(w, HOME.href())
            return

        # Queue must exist before publish — otherwise this tab misses its own join.
        # Subscribe first so this connection's queue exists before we publish presence
        # (avoids a gap where "join" and other events could be dropped for this client).
        async with relay.subscribe("*") as live:
            sse = SSE(w)
            game.join(user_id)
            # Same fan-out as clicks: all SSE clients patch so everyone sees the new player.
            relay.publish("join", user_id)  # wakes every open subscribe loop
            c.span.event("Player connected", {"user_id": user_id})
            c.span.attr("user_id", user_id)

            # Full tree patch — don't wait for relay; ship truth as soon as SSE opens.
            sse.patch_elements(home_view(user_id, game))

            # c.alive stops when the tab disconnects or the process shuts down.
            # First iteration (and every later one): relay fan-out — join included — drives patches.
            # No separate onload patch: `publish("join")` already queues for this subscriber.
            # `*` matches all topics; fine for demos — production often namespaces keys.
            async for subject, _ in c.alive(live):
                c.span.event("relay", {"subject": subject})
                sse.patch_elements(home_view(user_id, game))

        # Unsubscribed — then drop presence so leave is not delivered to this (dead) SSE queue.
        game.leave(user_id)
        relay.publish("leave", user_id)  # still subscribed — others get the update
        c.span.event("Player disconnected", {"user_id": user_id})

    return handler


def click(game: Game, relay: Relay[str]):
    """POST /click — toggle a cell; clients learn about it on SSE, not here."""

    async def handler(c: Context, w: Writer) -> None:
        user_id = await read_user_id(c)
        # Membership in user_colors means /subscribe succeeded for this tab.
        if user_id not in game.user_colors:
            responses.redirect(w, HOME.href())
            return

        cell_id_param = c.req.query.get("cellId")
        if cell_id_param is None:
            responses.redirect(w, HOME.href())
            return

        try:
            cell_id = int(cell_id_param)
        except ValueError:
            responses.redirect(w, HOME.href())
            return
        if not 0 <= cell_id < game.total_cells:
            responses.redirect(w, HOME.href())
            return

        c.span.attrs({"user_id": user_id, "cell_id": cell_id})

        # Send the HTTP response immediately (204 No Content). Same idea as returning
        # a response first in Starlette/FastAPI and then running a background task:
        # the client is done waiting, but this coroutine continues and may do I/O or
        # broadcast (here: update shared state and notify other tabs).
        responses.empty(w, 204)  # ack before fan-out — browser isn't waiting for HTML

        action = game.paint_cell(user_id, cell_id)
        c.span.event("Cell toggled", {"cell_id": cell_id, "action": action})

        # Fan-out: every open SSE subscription receives this; each client's
        # `subscribe` loop wakes up and calls `sse.patch_elements()`, so all users re-render.
        # Real-time in this demo—and the same pub/sub + patch pattern scales to
        # larger apps with a broker instead of in-process `Relay`.
        relay.publish("click", user_id)  # SSE tabs re-read Game and patch

    return handler


# =============================================================================
# 5. Bootstrap
# =============================================================================
#
# Composition root: one place to see how the app is wired. span.attrs here show up
# in startup telemetry (TTY, JSON, SQLite) before any request is handled.


async def bootstrap(app: App, span: Span):
    # `span` is the root bootstrap tracer; attr() annotates startup for TTY/JSON/SQLite sinks.
    game = Game()
    relay = Relay[str]()  # process-local fan-out between open /subscribe loops

    span.attrs(
        {
            "tiles.grid_size": game.grid_size,
            "tiles.total_cells": game.total_cells,
            "tiles.color_count": len(game.colors),
            "tiles.static_dir": str(ASSETS.directory),
        }
    )

    # Compression + caching cost is paid here; stats land on the startup trace.
    with span.step("static_assets") as s:
        static = StaticAssets(ASSETS)
        s.attrs(static.stats)
    static.register(app)

    # Queries — return or stream HTML
    app.get(HOME, home(game))
    app.get(SUBSCRIBE, subscribe(game, relay))
    # Commands — mutate Game, nudge relay; updates arrive on SSE
    app.post(CLICK, click(game, relay))
    yield
