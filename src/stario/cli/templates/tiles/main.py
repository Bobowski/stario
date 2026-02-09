import asyncio
import random
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

# Core imports:
# - Context: request context (headers, query params, signals, tracing)
# - Writer: response writer (html, sse patches, redirects, status codes)
# - Stario: the app instance - register routes and serve
# - at/data: Datastar helpers for reactive attributes (@get, @post, data-*)
# - asset: fingerprinted static file names for cache busting
# - Relay: in-process pub/sub for broadcasting between connections
from stario import (
    Context,
    JsonTracer,
    Relay,
    RichTracer,
    Stario,
    Writer,
    asset,
    at,
    data,
)

# HTML elements - call like functions, first arg is attrs dict (optional)
# Example: Div({"class": "foo"}, "child1", child2) -> <div class="foo">child1...</div>
from stario.html import (
    H1,
    Body,
    Div,
    Head,
    Html,
    Li,
    Link,
    Meta,
    P,
    Script,
    Span,
    Title,
    Ul,
)
from stario.toys import toy_inspector

# =============================================================================
# Configuration
# =============================================================================

GRID_SIZE = 5
COLORS = [
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
]

# =============================================================================
# State
# In production, you'd use a database. For this demo, we'll use in-memory.
# =============================================================================

# Board state: cell_id -> color hex
_total_cells = GRID_SIZE * GRID_SIZE
_initial_cells = random.sample(range(_total_cells), int(_total_cells * 0.6))
board: dict[int, str] = {cell: random.choice(COLORS) for cell in _initial_cells}

# Track connected users (we derive their color from user_id hash)
users: set[str] = set()

# Relay: lightweight pub/sub for broadcasting between SSE connections.
# When state changes, publish to notify all subscribers to re-render.
# Generic type [None] means no payload - just a "something changed" signal.
relay = Relay[str]()


def color_for_user(user_id: str) -> str:
    """Deterministically derive color from user_id."""
    return COLORS[hash(user_id) % len(COLORS)]


# =============================================================================
# Views
# =============================================================================
#
# Views are pure functions that return HTML elements.
# No side effects - just data in, HTML out. Easy to test and reason about.
#
# Pattern: Build your UI as a tree of element calls.
# Datastar patches the DOM efficiently, so re-rendering the whole page is cheap.


def page(*children):
    """Base HTML page with Datastar and styles."""
    return Html(
        {"lang": "en"},
        Head(
            Meta({"charset": "UTF-8"}),
            Meta(
                {"name": "viewport", "content": "width=device-width, initial-scale=1"}
            ),
            Title("Tiles - Stario App"),
            # asset() returns fingerprinted filename (e.g., style.abc123.css)
            # for cache busting - files change, URLs change, caches invalidate
            Link({"rel": "stylesheet", "href": "/static/" + asset("css/style.css")}),
            Script({"type": "module", "src": "/static/" + asset("js/datastar.js")}),
        ),
        Body(*children),
    )


def cell_view(cell_id: int, color: str | None):
    """Single cell - colored if painted, empty otherwise."""
    # Note: style can be a dict - it gets converted to "background-color: #abc123"
    if color:
        return Div(
            {
                "class": "cell painted",
                "data-cell-id": str(cell_id),
                "style": {"background-color": color},
            }
        )
    return Div({"class": "cell", "data-cell-id": str(cell_id)})


def board_view():
    """The game board - a grid of cells."""
    rows = []
    for row in range(GRID_SIZE):
        cells = []
        for col in range(GRID_SIZE):
            cell_id = row * GRID_SIZE + col
            color = board.get(cell_id)
            cells.append(cell_view(cell_id, color))
        rows.append(Div({"class": "row"}, *cells))

    is_complete = len(board) == GRID_SIZE * GRID_SIZE
    board_class = "board complete" if is_complete else "board"

    # data.on() creates data-on-click attribute for Datastar
    # The JS runs client-side: @post is Datastar's action syntax
    # It automatically includes signals (like user_id) in the request
    return Div(
        {"id": "board", "class": board_class},
        data.on(
            "click",
            """
            let id = evt.target.dataset.cellId;
            if (id) @post(`/click?cellId=${id}`);
            """,
        ),
        *rows,
    )


def users_view():
    """List of connected users shown as colored swatches."""
    if not users:
        return Ul({"id": "users", "class": "users-list"}, Li({"class": "empty"}, "..."))

    items = [
        Li(Span({"class": "swatch", "style": {"background-color": color}}))
        for color in [color_for_user(uid) for uid in users]
    ]
    return Ul({"id": "users", "class": "users-list"}, *items)


def info_panel_view(user_id: str):
    """Info panel showing user count and player color."""

    user_color = color_for_user(user_id) if user_id else "#ccc"
    return Div(
        {"id": "info", "class": "info-panel"},
        Div(
            {"class": "info-row"},
            Span({"class": "info-label"}, "Players"),
            users_view(),
        ),
        Div(
            {"class": "info-row"},
            Span({"class": "info-label"}, "Your color"),
            Span({"class": "swatch", "style": {"background-color": user_color}}),
        ),
    )


def home_view(user_id: str):
    """Full home page - user_id passed via signals for SSE."""
    return page(
        Div(
            {"id": "home", "class": "container"},
            # Signals: client-side reactive state synced with server.
            # ifmissing=True means: only set if not already present (preserves state on re-render)
            # Signals are automatically sent with every @get/@post request.
            data.signals({"user_id": user_id}, ifmissing=True),
            # data.init() runs on element mount - here it opens SSE connection.
            data.init(at.get("/subscribe", retry="always")),
            # Main content
            H1("Tiles - Stario App"),
            P(
                {"class": "subtitle"},
                "Click cells to paint. Everyone sees changes live!",
            ),
            info_panel_view(user_id),
            board_view(),
            toy_inspector("bottom-right"),
        ),
    )


# =============================================================================
# Handlers
# =============================================================================
#
# Every handler receives:
# - Context (c): request data, signals, query params, headers, tracing
# - Writer (w): send responses - html(), patch(), redirect(), empty()
#
# Handlers are async - use await for I/O (database, signals parsing, etc.)


# Define expected signals as a dataclass - c.signals() parses and validates
@dataclass
class HomeSignals:
    user_id: str


async def home(c: Context, w: Writer) -> None:
    """Serve the home page with a fresh user_id."""
    # Generate unique user_id, passed to client via signals
    user_id = str(uuid.uuid4())[:8]
    # Set context state for reactive attributes
    c["user_id"] = user_id

    # w.html() sends a full HTML response (Content-Type: text/html)
    w.html(home_view(user_id))


async def subscribe(c: Context, w: Writer) -> None:
    """
    SSE endpoint - subscribe to real-time updates.

    This is the heart of real-time: clients connect here and receive
    DOM patches whenever state changes. Pattern is simple:
    1. Client connects (data.init triggers @get)
    2. Server loops, sending patches when relay fires
    3. Datastar merges patches into DOM
    4. On disconnect, cleanup runs (after the loop exits)
    """
    # c.signals() parses signals from request (sent automatically by Datastar @get/@post)
    signals = await c.signals(HomeSignals)
    if not signals.user_id:
        c("No user id", {"hint": "user had to change some thing manually"})
        w.redirect("/")
        return

    # Add user and notify everyone (including this new user)
    users.add(signals.user_id)
    relay.publish("join", signals.user_id)
    c("on_join", {"user_id": signals.user_id})
    c["user_id"] = signals.user_id

    # w.patch() sends SSE with Datastar merge fragments
    # Elements matched by id are updated in-place
    w.patch(home_view(signals.user_id))
    c("onload patch")

    # w.alive() wraps an async iterator and yields until client disconnects.
    # relay.subscribe() yields each time someone publishes to "update" topic.
    # When client disconnects, the loop exits cleanly (no exception).
    async for event, user_id in w.alive(relay.subscribe("*")):
        c("on_event", {"event": event, "user_id": user_id})
        w.patch(home_view(signals.user_id))

    # Code after the loop runs on disconnect - perfect for cleanup
    users.discard(signals.user_id)
    relay.publish("leave", signals.user_id)
    c("on_leave", {"user_id": signals.user_id})


async def click(c: Context, w: Writer) -> None:
    """Handle cell click - update board and broadcast."""
    # Signals come from Datastar (auto-included in @post requests)
    signals = await c.signals(HomeSignals)
    if not signals.user_id or signals.user_id not in users:
        c("No user id or user not connected", {"user_id": signals.user_id})
        w.redirect("/")
        return

    # Query params accessed via c.req.query
    cell_id_param = c.req.query.get("cellId")
    if cell_id_param is None:
        c("No cell id", {"hint": "pass cellId as query parameter"})
        w.redirect("/")
        return

    cell_id = int(cell_id_param)

    # Set context state for reactive attributes
    c["user_id"] = signals.user_id
    c["cell_id"] = cell_id

    # Respond immediately with 204 No Content.
    # Important: code after w.empty() still runs! The response is sent,
    # but the handler continues - useful for fire-and-forget patterns.
    w.empty(204)

    user_color = color_for_user(signals.user_id)

    # Toggle: if same color, clear it; otherwise paint
    if board.get(cell_id) == user_color:
        board.pop(cell_id, None)
    else:
        board[cell_id] = user_color

    # Publish triggers all relay.subscribe("*") iterators to yield.
    # Each SSE connection will re-render and send a patch to its client.
    relay.publish("click", signals.user_id)


async def main():
    # Pick tracer based on environment:
    # - RichTracer: pretty console output for local dev
    # - JsonTracer: structured JSON logs for production
    if "--local" in sys.argv or sys.stdout.isatty():
        tracer = RichTracer()
        host = "127.0.0.1"
        port = 8000
        workers = 1
    else:
        tracer = JsonTracer()
        host = "0.0.0.0"
        port = 8000
        workers = 4

    # Create app with tracer for observability
    with tracer:
        app = Stario(tracer)

        # Static files: app.assets(url_prefix, directory)
        # Files are fingerprinted - use asset("path") to get versioned URL
        app.assets("/static", Path(__file__).parent / "static")

        # Register routes: app.{method}(path, handler)
        # Handler signature is always: async def handler(c: Context, w: Writer)
        app.get("/", home)
        app.get("/subscribe", subscribe)  # SSE endpoint for real-time
        app.post("/click", click)

        # Start the server - blocks until shutdown
        await app.serve(host=host, port=port, workers=workers)


if __name__ == "__main__":
    asyncio.run(main())
