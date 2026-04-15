import random
import uuid
from pathlib import Path

# Stario apps center on: Context (per-request), Writer (transport), and plain Python
# instead of a template language. HTML lives under `stario.html`; importing that
# submodule as `h` keeps tag names (`h.Div`, `h.Span`) separate from framework
# symbols like telemetry `Span` (same name as <span>, different concept).
from stario import (
    App,
    Context,
    Relay,
    Span,
    StaticAssets,
    Writer,
)
from stario import (
    datastar as ds,
)
from stario import (
    html as h,
)
from stario import (
    responses as responses,
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
# In production, you'd use a database. Here, module-level dict/set keep
# everything in one file so the flow is easy to read from top to bottom.
#
# For a larger layout (handlers vs views, shared state, SQLite), see the chat
# example: `shared/stario/examples/chat-room` in the stario repo.
# =============================================================================

# Board state: cell_id -> color hex
_total_cells = GRID_SIZE * GRID_SIZE
_initial_cells = random.sample(range(_total_cells), int(_total_cells * 0.6))
board: dict[int, str] = {cell: random.choice(COLORS) for cell in _initial_cells}

# Connected users (color is derived deterministically from user_id in the view)
users: set[str] = set()

# Relay: in-process pub/sub between handlers (no Redis). SSE uses
# ``async with relay.subscribe(...)`` so the queue exists before ``publish``;
# ``click`` / join / leave publish. [str] is the payload type on join/leave (user id).
relay = Relay[str]()


def color_for_user(user_id: str) -> str:
    """Pick a display color for ``user_id`` (stable for the same id in one process)."""
    return random.Random(user_id).choice(COLORS)


# =============================================================================
# Views
# =============================================================================
#
# Views are pure functions that build HTML using the callables in
# `stario.html` (each tag is a function-like object: Div, Span, …). You nest
# them like a normal function call tree; the framework turns that tree into
# markup when you pass it to `responses.html()` or
# `ds.sse.patch_elements()`, with escaping applied
# to ordinary strings so user-controlled text is safe by default.
#
# This file imports `stario.html` as `h` so tag constructors read as `h.Div`,
# `h.Span`, etc., and do not collide with unrelated names such as telemetry
# `Span` from `stario` (the tracing handle, not the HTML element).
#
# For deliberate raw HTML, see `stario.html.SafeString`.


def page(app: App, *children):
    """Base HTML page with Datastar and styles served from static assets."""
    return h.HtmlDocument(
        {"lang": "en"},
        h.Head(
            h.Meta({"charset": "UTF-8"}),
            h.Meta(
                {"name": "viewport", "content": "width=device-width, initial-scale=1"}
            ),
            h.Title("Tiles - Stario App"),
            h.Link({"rel": "stylesheet", "href": app.url_for("static:css/style.css")}),
            h.Script({"type": "module", "src": app.url_for("static:js/datastar.js")}),
        ),
        h.Body(*children),
    )


def cell_view(cell_id: int, color: str | None):
    """Single cell - colored if painted, empty otherwise."""
    # `style` may be a dict; stario turns it into a CSS declaration string.
    if color:
        return h.Div(
            {
                "class": "cell painted",
                "data-cell-id": str(cell_id),
                "style": {"background-color": color},
            }
        )
    return h.Div({"class": "cell", "data-cell-id": str(cell_id)})


def board_view(app: App):
    """The game board - a grid of cells."""
    rows = []
    for row in range(GRID_SIZE):
        cells = []
        for col in range(GRID_SIZE):
            cell_id = row * GRID_SIZE + col
            color = board.get(cell_id)
            cells.append(cell_view(cell_id, color))
        rows.append(h.Div({"class": "row"}, *cells))

    is_complete = len(board) == GRID_SIZE * GRID_SIZE
    board_class = "board complete" if is_complete else "board"

    # ds.on -> data-on:* attributes; `@post` is Datastar's fetch helper (signals ride along).
    return h.Div(
        {"id": "board", "class": board_class},
        ds.on(
            "click",
            f"""
            let id = evt.target.dataset.cellId;
            if (id) {{ @post(`{app.url_for("click")}?cellId=${{id}}`); }}
            """,
        ),
        *rows,
    )


def users_view():
    """List of connected users shown as colored swatches."""
    if not users:
        return h.Ul(
            {"id": "users", "class": "users-list"},
            h.Li({"class": "empty"}, "..."),
        )

    items = [
        h.Li(h.Span({"class": "swatch", "style": {"background-color": color}}))
        for color in [color_for_user(uid) for uid in users]
    ]
    return h.Ul({"id": "users", "class": "users-list"}, *items)


def info_panel_view(user_id: str):
    """Info panel showing user count and player color."""

    user_color = color_for_user(user_id) if user_id else "#ccc"
    return h.Div(
        {"id": "info", "class": "info-panel"},
        h.Div(
            {"class": "info-row"},
            h.Span({"class": "info-label"}, "Players"),
            users_view(),
        ),
        h.Div(
            {"class": "info-row"},
            h.Span({"class": "info-label"}, "Your color"),
            h.Span({"class": "swatch", "style": {"background-color": user_color}}),
        ),
    )


def home_view(user_id: str, app: App):
    """Full home page - user_id passed via signals for SSE."""
    return page(
        app,
        h.Div(
            {"id": "home", "class": "container"},
            # Signals: client-held JSON synced with the server; every Datastar @get/@post includes them.
            # ifmissing=True: set defaults only when absent so reconnects don't clobber client state.
            ds.signals({"user_id": user_id}, ifmissing=True),
            # ds.init runs once when the node mounts — here it opens the SSE stream (`@get`).
            ds.init(ds.get(app.url_for("subscribe"), retry="always")),
            h.H1("Tiles - Stario App"),
            h.P(
                {"class": "subtitle"},
                "Click cells to paint. Everyone sees changes live!",
            ),
            info_panel_view(user_id),
            board_view(app),
            toy_inspector("bottom-right"),
        ),
    )


# =============================================================================
# Handlers
# =============================================================================
#
# Signature is always async def handler(c: Context, w: Writer).
# Use c for request data (query, signals, span); use w for the response edge.


async def home(c: Context, w: Writer) -> None:
    """Serve the home page with a fresh user_id."""
    user_id = str(uuid.uuid4())[:8]
    c.span.attr("user_id", user_id)

    responses.html(w, home_view(user_id, c.app))


async def subscribe(c: Context, w: Writer) -> None:
    """
    Long-lived SSE stream: stay subscribed to relay while the tab is open, push fresh
    HTML whenever shared state changes; tear down presence after unsubscribing so leave
    is not delivered to a closed connection.
    """
    # Reads Datastar signals from the client and returns plain JSON-shaped data.
    signals = await ds.read_signals(c.req)

    my_user_id = str(signals.get("user_id", ""))
    if not my_user_id:
        c.span.event("No user id", {"hint": "user had to change some thing manually"})
        responses.redirect(w, c.app.url_for("home"))
        return

    # Subscribe first so this connection's queue exists before we publish presence
    # (avoids a gap where "join" and other events could be dropped for this client).
    async with relay.subscribe("*") as live:
        users.add(my_user_id)
        # Same fan-out as clicks: all SSE clients patch so everyone sees the new player.
        relay.publish("join", my_user_id)
        c.span.event("on_join", {"user_id": my_user_id})
        c.span.attr("user_id", my_user_id)

        # First iteration (and every later one): relay fan-out — join included — drives patches.
        # No separate onload patch: ``publish("join")`` already queues for this subscriber.
        # `*` matches all topics; fine for demos — production often namespaces keys.
        async for event, from_user_id in w.alive(live):
            c.span.event("on_event", {"event": event, "from_user_id": from_user_id})
            ds.sse.patch_elements(w, home_view(my_user_id, c.app))

    # Unsubscribed — then drop presence so leave is not delivered to this (dead) SSE queue.
    users.discard(my_user_id)
    relay.publish("leave", my_user_id)
    c.span.event("on_leave", {"user_id": my_user_id})


async def click(c: Context, w: Writer) -> None:
    """Handle cell click - update board and broadcast."""
    signals = await ds.read_signals(c.req)
    user_id = str(signals.get("user_id", ""))
    if not user_id or user_id not in users:
        c.span.event("No user id or user not connected", {"user_id": user_id})
        responses.redirect(w, c.app.url_for("home"))
        return

    cell_id_param = c.req.query.get("cellId")
    if cell_id_param is None:
        c.span.event("No cell id", {"hint": "pass cellId as query parameter"})
        responses.redirect(w, c.app.url_for("home"))
        return

    cell_id = int(cell_id_param)

    c.span.attrs({"user_id": user_id, "cell_id": cell_id})

    # Send the HTTP response immediately (204 No Content). Same idea as returning
    # a response first in Starlette/FastAPI and then running a background task:
    # the client is done waiting, but this coroutine continues and may do I/O or
    # broadcast (here: update shared state and notify other tabs).
    responses.empty(w, 204)
    c.span.event("command.accepted", {"cell_id": cell_id})

    user_color = color_for_user(user_id)

    if board.get(cell_id) == user_color:
        board.pop(cell_id, None)
    else:
        board[cell_id] = user_color

    # Fan-out: every open SSE subscription receives this; each client's
    # `subscribe` loop wakes up and calls `ds.sse.patch_elements()`, so all users re-render.
    # Real-time in this demo—and the same pub/sub + patch pattern scales to
    # larger apps with a broker instead of in-process `Relay`.
    relay.publish("click", user_id)
    c.span.event("relay.published", {"topic": "click", "user_id": user_id})


async def bootstrap(app: App, span: Span) -> None:
    # `span` is the root bootstrap tracer; attr() annotates startup for TTY/JSON/SQLite sinks.
    static_dir = Path(__file__).parent / "static"
    static_dir_display = static_dir.relative_to(Path.cwd())
    span.attr("static_dir", str(static_dir_display))
    app.mount("/static", StaticAssets(static_dir, name="static"))

    app.get("/", home, name="home")
    app.get("/subscribe", subscribe, name="subscribe")
    app.post("/click", click, name="click")
