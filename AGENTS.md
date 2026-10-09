# Stario app guide

This file teaches a person or an agent how to build a Stario app. Copy it to the root of every Stario repo. Read it before you write code. It is the whole default. When the developer asks for something else, do that instead.

- Docs: https://stario.dev/docs
- Deeper context: https://stario.dev/llms-full.txt
- Optional stacks (SQLite, Tailwind, adding a page): https://stario.dev/skills/
- Reference app: https://github.com/bobowski/stario/tree/main/examples/chat-room

Stario is Python hypermedia. A handler writes HTML. Datastar morphs the page in the browser. A long-lived SSE stream keeps a page live. State lives on the server.

## Start a project

Python 3.14 and `uv`:

```bash
uv init --app my-app
cd my-app
uv add "stario>=4,<5"
uv add --dev ruff pyright pytest pytest-asyncio
curl -o AGENTS.md https://stario.dev/AGENTS.md
mkdir -p app/common app/features static/css static/js tests
touch app/__init__.py app/common/__init__.py app/features/__init__.py tests/__init__.py static/css/style.css
curl -o static/js/datastar.js https://cdn.jsdelivr.net/gh/starfederation/datastar@v1.0.2/bundles/datastar.js
```

Add to `pyproject.toml`:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]
asyncio_mode = "auto"
```

Run and check:

```bash
uv run stario watch app.main:bootstrap   # dev server with reload on http://127.0.0.1:8000
uv run ruff check . && uv run ruff format .
uv run pyright
uv run pytest
```

Stario does not read `.env`. Configure with environment variables. `STARIO_PORT` changes the port.

## Layout: one folder per page

Every screen of the app is one feature folder. A screen is a page or a resource with its own URL: `/`, `/settings`, `/rooms/{room_id}`. The folder owns the URLs, the data, the views, and the handlers of that screen. The number of folders tracks the number of screens. When a screen goes away, delete its folder.

```text
app/
  main.py             bootstrap: build dependencies, register every feature
  config.py           frozen Config dataclass, from_env(), read once in bootstrap
  assets.py           ASSETS = Assets(...), STYLE_CSS, DATASTAR_JS
  common/
    shell.py          page(title, body): the <html> document, @baked
  features/
    <screen>/
      urls.py         Route values for this screen
      data.py         SCHEMA + query functions, when the screen owns tables
      views.py        render(state) -> full document
      handlers.py     handlers + register
static/
  js/datastar.js      vendored client
  css/                stylesheets
tests/
  conftest.py         TestClient(bootstrap) fixture
  test_<screen>.py    one file per screen
AGENTS.md
```

Rules for the tree:

- Use this layout from the first commit, also with one screen. There is no later migration.
- `urls.py`, `views.py`, and `handlers.py` always exist. `data.py` exists when the screen owns tables. Each folder has an `__init__.py`. No other files by default. Add `models.py` or `subjects.py` only when `data.py` or `handlers.py` has become hard to read.
- A feature is self-contained. It owns its tables, its HTML, and its handler logic.
- A feature may import another feature's `urls.py` for links and another feature's `data.py` for reads. Imports flow one way. Do not import views across features.
- `common/` holds behaviour every screen shares: the document shell, identity, time formatting. It does not hold borrowed views or borrowed queries.
- Two screens with similar HTML keep their own copies. Merge later as an optimization, when the copies have proven identical. Do not merge by default.
- `app/db.py` exists only when more than one feature needs the same connection object. Keep it thin: connect, transaction, apply schema.

## Stario invariants

Stario is not ASGI. Do not mount it in Uvicorn, Hypercorn, FastAPI, or Starlette. Do not return response objects. Run with a bootstrap spec: `stario watch app.main:bootstrap`.

Default imports:

```python
from stario import App, Context, HttpException, Relay, Route, Span, Writer, responses
from stario.datastar import SSE, at, data, read_signals
from stario.markup import HtmlElement, baked, classes
from stario.markup import html as h
```

Import order in every file: standard library, then third-party (`stario`, `pytest`), then `app`. `ruff` enforces it.

Rules:

- `bootstrap` is `async def bootstrap(app: App, span: Span)` with exactly one `yield`. Build dependencies, attach assets, and register routes before `yield`. Clean up after `yield`.
- A handler is `async def handler(c: Context, w: Writer) -> None`. It completes the response through `responses.html`, `responses.empty`, `responses.redirect`, or `SSE(w)`. It completes the response once.
- A `Route` is one method on one path: `Route("GET /rooms/{room_id}")` or `Route("POST", ROOM_PATH + "/send")`. Register with `app.add(route, handler)`. Build links with `route.href(room_id)` or `route.href(room_id=x)`. There is no `url_for`.
- `c.match.params["room_id"]` reads a path capture. `c.req.query` reads the query string.
- A long-lived loop iterates `c.alive(source)`. The loop ends on disconnect or shutdown. Cleanup after the loop always runs.
- Background work uses `c.app.create_task(...)`, not bare `asyncio.create_task`.
- Raise `HttpException(400, "message")` for a bad request. Raise `HttpException(404, "message")` for a missing row.
- Framework JSON goes through `stario.json`. Do not call `json.dumps` for signals or responses.
- Stario runs on one event loop in one process. Shared state is a plain object passed into the feature. No globals.

## The default architecture

Decide per screen:

- A screen with no action is a static page. One `GET` handler. `responses.html(w, render(state))`. Done.
- A screen with any action is live. Three kinds of routes. This is CQRS over HTTP.

```text
GET  /board         first document
GET  /board/live    SSE stream that stays open and repaints the page
POST /board/...     command: validate, reply 204, change state, publish
```

The stream is the only place that paints. A command never returns HTML. If a command returned HTML, the tab that clicked would race the stream, and other tabs would stay stale.

The loop:

1. `GET /board` renders `render(state)`.
2. The page opens `GET /board/live` on load with `retry="always"`.
3. The stream sends `render(state)` once, then waits for a notification.
4. A command validates the input, replies `responses.empty(w)`, changes the store, publishes a notification.
5. Every open stream wakes, reads state again, sends `render(state)`. Datastar morphs the page.

The notification path is `Relay` in one process. A store with a watch feature can replace it (see the litewriter skill). For more than one process, swap `Relay` for a broker and keep the same three routes.

A failed command is still a view of state. Store the failure, publish, and let `render` show the message. Reply `400` only when the request is malformed.

A command that moves the user to another screen does not reply 204. From a Datastar action (`at.post`) it replies `SSE(w).navigate(ROOM.href(room.id))`. From a plain HTML form `POST` it replies `responses.redirect(w, ROOM.href(room.id), 303)`. Neither returns HTML.

## A complete live screen

`app/features/board/urls.py`:

```python
from stario import Route

BOARD = Route("GET /board")
BOARD_LIVE = Route("GET /board/live")
PAINT = Route("POST /board/{tile_id}/paint")
```

`app/features/board/data.py`:

```python
import sqlite3
from dataclasses import dataclass

SCHEMA = """
CREATE TABLE IF NOT EXISTS tiles (
    id INTEGER PRIMARY KEY,
    color TEXT NOT NULL DEFAULT '#ffffff'
);
INSERT OR IGNORE INTO tiles (id) VALUES (1), (2), (3), (4), (5), (6), (7), (8), (9);
"""


@dataclass(frozen=True, slots=True)
class Tile:
    id: int
    color: str


def list_tiles(conn: sqlite3.Connection) -> list[Tile]:
    rows = conn.execute("SELECT id, color FROM tiles ORDER BY id").fetchall()
    return [Tile(*row) for row in rows]


def set_color(conn: sqlite3.Connection, tile_id: int, color: str) -> None:
    with conn:
        conn.execute("UPDATE tiles SET color = ? WHERE id = ?", (color, tile_id))
```

`app/features/board/views.py`:

```python
from stario.datastar import at, data
from stario.markup import HtmlElement, classes
from stario.markup import html as h

from app.common.shell import page
from app.features.board.data import Tile
from app.features.board.urls import BOARD_LIVE, PAINT


def render(tiles: list[Tile]) -> HtmlElement:
    return page(
        "Board",
        h.Main(
            {"id": "board"},
            data.signals({"color": "#000000"}, if_missing=True),
            data.init(at.get(BOARD_LIVE.href(), retry="always")),
            h.Label("Color", h.Input({"type": "color"}, data.bind("color"))),
            h.Div(
                classes("grid"),
                *[
                    h.Button(
                        {"style": f"background:{tile.color}", "aria-label": f"Tile {tile.id}"},
                        data.on("click", at.post(PAINT.href(tile.id))),
                    )
                    for tile in tiles
                ],
            ),
        ),
    )
```

`app/features/board/handlers.py`:

```python
import re
import sqlite3

from stario import App, Context, HttpException, Relay, Writer, responses
from stario.datastar import SSE, read_signals

from app.features.board import data
from app.features.board.urls import BOARD, BOARD_LIVE, PAINT
from app.features.board.views import render

CHANGED = "board.changed"


class Board:
    def __init__(self, conn: sqlite3.Connection, relay: Relay[str]) -> None:
        self.conn = conn
        self.relay = relay

    async def show(self, c: Context, w: Writer) -> None:
        responses.html(w, render(data.list_tiles(self.conn)))

    async def live(self, c: Context, w: Writer) -> None:
        async with self.relay.subscribe(CHANGED) as events:
            sse = SSE(w)
            sse.patch_elements(render(data.list_tiles(self.conn)))
            async for _ in c.alive(events):
                sse.patch_elements(render(data.list_tiles(self.conn)))

    async def paint(self, c: Context, w: Writer) -> None:
        signals = await read_signals(c.req)
        color = str(signals.get("color", ""))
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            raise HttpException(400, "Color must be #rrggbb")
        responses.empty(w)
        data.set_color(self.conn, int(c.match.params["tile_id"]), color)
        self.relay.publish(CHANGED, "paint")

    def register(self, app: App) -> None:
        app.add(BOARD, self.show)
        app.add(BOARD_LIVE, self.live)
        app.add(PAINT, self.paint)
```

`app/main.py`:

```python
import sqlite3

from stario import App, Relay, Span

from app.assets import ASSETS
from app.config import Config
from app.features.board import data as board_data
from app.features.board.handlers import Board


async def bootstrap(app: App, span: Span):
    config = Config.from_env()
    conn = sqlite3.connect(config.db_path)
    conn.executescript(board_data.SCHEMA)
    relay: Relay[str] = Relay()

    span.attrs(await ASSETS.attach(app))
    Board(conn, relay).register(app)
    yield
    conn.close()
```

`app/common/shell.py`:

```python
from stario.datastar import ModuleScript
from stario.markup import HtmlElement, baked
from stario.markup import html as h

from app.assets import DATASTAR_JS, STYLE_CSS


@baked
def page(title: str, body: HtmlElement) -> HtmlElement:
    return h.HtmlDocument(
        {"lang": "en"},
        h.Head(
            h.Meta({"charset": "utf-8"}),
            h.Meta({"name": "viewport", "content": "width=device-width, initial-scale=1"}),
            h.Title(title),
            h.Link({"rel": "stylesheet", "href": STYLE_CSS}),
            ModuleScript(DATASTAR_JS),
        ),
        h.Body(body),
    )
```

`app/assets.py`:

```python
from pathlib import Path

from stario import Assets

ASSETS = Assets(Path(__file__).resolve().parents[1] / "static", url_prefix="/static")
STYLE_CSS = ASSETS.href("css/style.css")
DATASTAR_JS = ASSETS.href("js/datastar.js")
```

`app/config.py`:

```python
import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Config:
    db_path: str

    @classmethod
    def from_env(cls) -> Config:
        return cls(db_path=os.environ.get("APP_DB_PATH", "app.sqlite3"))
```

Note the two names called `data`. In `handlers.py`, `data` is the feature's `data.py`. In `views.py`, `data` is `stario.datastar.data`. They do not meet in one file.

## Handlers

Two shapes. Pick by size.

Up to three handlers with different dependencies: a factory per handler.

```python
def show_about(version: str):
    async def handler(c: Context, w: Writer) -> None:
        responses.html(w, render(version))

    return handler


def register_about(app: App, version: str) -> None:
    app.add(ABOUT, show_about(version))
```

Four or more handlers, or a stream plus commands sharing `db` and `relay`: one class. `__init__` takes the dependencies. Each handler is a method. `register(app)` adds the routes. Every handler stays reachable by name, which a test or another feature can use.

Inside a handler:

- Read input first: `c.match.params`, `c.req.query`, `await read_signals(c.req)`.
- Validate. Raise `HttpException` when the request is malformed.
- Reply. A command replies `responses.empty(w)` before the work when the work is slow.
- Change state. Publish.

A subscribe handler opens the subscription before it publishes anything this connection must see. Cleanup after `async for` runs on disconnect and on shutdown.

## Render from state

Each live screen has one function, `render(state) -> HtmlElement`, that returns the full document. The first `GET` and every stream wake call the same function. `ui = f(state)` stays visible.

- The stream sends the whole document: `sse.patch_elements(render(state))`. Datastar morphs the live DOM. Compression on the open stream makes repeated HTML cheap. Do not start with small targeted patches. Narrow a patch only after you have measured a cost.
- `render` does not fetch. The handler loads state and passes it in. State is a frozen dataclass or the rows the queries return.
- The element that carries `data.init(...)` and `data.signals(...)` keeps the same tag and `id` in every render. Datastar morphs it in place, so the stream is not reopened. A changed tag or `id` would replace the element and reconnect.
- The document shell is `@baked`. A baked function compiles once. Inside a baked function, a parameter is a whole child or a whole attribute value. Loops, conditions, and string formatting happen in the caller.
- Write the HTML tree inline inside `render`. A helper view exists for a block that repeats in a loop. A helper view does not exist for one call site. Do not build a helper per element.
- Repetition inside one feature is merged inside that feature. Repetition across features is left alone until it has proven stable.

## Signals and UI state

Signals are form input. Nothing else by default.

- `data.bind("color")` binds an input to a signal. `read_signals(c.req)` reads every signal on each `@get` or `@post`.
- One `data.signals({...}, if_missing=True)` block per page, on the element that opens the stream. `if_missing=True` keeps what the user typed when the page morphs again.
- The server never trusts a signal. Every command validates the signal and reads truth from the store.
- Identity and selection live in the URL: `/rooms/{room_id}`, `?tab=open`. Not in a signal. A link or `sse.navigate(url)` changes them.
- The stream patches HTML. It does not patch signals. One exception: after a command consumes an input, the stream or the command may reset that input with `sse.patch_signals({"message": ""})`.
- UI state the user controls must survive a morph. A morph arrives at any moment. If it closes the dialog the user just opened, the app feels broken. Protect that state in the HTML:
  - `data.preserve_attr("open")` on `<dialog>` and `<details>`. The morph keeps the current value.
  - `data.ignore_morph()` on a subtree the client owns, such as a text editor with its own state.
  - Do not render `{"open": True}` from the server for a dialog the user opens.
- Signal names are `snake_case` on the wire. Nested state is a nested object, not a dotted name.

## Code shape

Fewer functions, not thinner ones. The work lives where you can read it.

- Inline a function whose body is one call: `return other(...)`, one `sse.patch_elements(...)`, one `responses.html(...)`.
- Inline a private alias that exists to rename a call.
- Do not extract a helper because a line is long. Do not extract a helper for one call site.
- Keep a function when it has two or more call sites, when it is a loop body, or when it does I/O you want to test alone. Branches alone do not earn a name. One call site inlines, branches included.
- Do not add a backwards-compatible alias. Rename, then fix the callers.
- No comments that restate the code. A comment explains a non-obvious reason.
- Name query functions after the SQL verb: `list_*` returns many, `get_*` returns one or `None`, `add_*`, `set_*`, `remove_*` mutate.
- Delete code with its screen. A removed page leaves no folder, no route, no test file.

## Tests

Tests run against the production `bootstrap` through `TestClient`. There is no second composition root.

`tests/conftest.py`:

```python
import pytest
from stario.testing import TestClient

from app.main import bootstrap


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "app.sqlite3"))
    async with TestClient(bootstrap) as c:
        yield c
```

Each screen has one test file with two kinds of tests:

1. The page loads. `GET` the route, assert `200`, assert text the page must show.
2. A command repaints the stream. Open the stream, send the command, read the next event.

```python
from app.features.board.urls import BOARD, BOARD_LIVE, PAINT


async def test_board_loads(client):
    r = await client.get(BOARD.href())
    assert r.status_code == 200
    assert 'id="board"' in r.text


async def test_paint_repaints_the_stream(client):
    headers = {"accept-encoding": "identity"}
    async with client.stream("GET", BOARD_LIVE.href(), headers=headers) as live:
        events = live.iter_events()
        first = await anext(events)
        assert 'id="board"' in first["data"]

        r = await client.post(PAINT.href(1), json={"color": "#ff0000"})
        assert r.status_code == 204

        repaint = await anext(events)
        assert "#ff0000" in repaint["data"]


async def test_paint_rejects_bad_color(client):
    r = await client.post(PAINT.href(1), json={"color": "red"})
    assert r.status_code == 400
```

`client.post(url, json=...)` sends the JSON object as Datastar signals. `client.stream(...)` yields once headers are in. `iter_events()` yields SSE events as dicts with `event` and `data`. Leaving the `async with` disconnects the stream and ends the handler.

Count the events. A stream that publishes on join (presence) repaints once more before the command repaint. Read that event first, then send the command. A command that replies `SSE(w).navigate(url)` returns `200` with `text/event-stream`; assert `"window.location" in r.text` and the target path.

Test the data layer directly only when a query has logic of its own. Point tests at a temporary database: in the `client` fixture, `monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "app.sqlite3"))` before `TestClient(bootstrap)` opens. `Config.from_env()` runs inside `bootstrap`, so each test gets its own file.

## Common mistakes

| Avoid | Prefer |
| --- | --- |
| Flat `app/routes.py`, `app/handlers.py` | `app/features/<screen>/` from the first commit |
| A folder named after a layer (`app/views/`) | A folder named after a screen (`app/features/settings/`) |
| FastAPI, Starlette, ASGI response objects | `App`, `Route`, `Context`, `Writer`, `responses.*` |
| `app.get(path, handler)` | `Route("GET /path")` and `app.add(route, handler)` |
| `url_for(...)` | `ROUTE.href(...)` |
| `bootstrap` without `yield` | One async generator with one `yield` |
| A command that returns HTML | A command that replies 204 and publishes |
| `patch_elements(fragment, selector=...)` as the first version | `patch_elements(render(state))` for the whole document |
| A signal that holds a row, a list, or an id | The row in the store, the id in the URL |
| `{"open": True}` rendered for a user-opened dialog | `data.preserve_attr("open")` |
| `Writer.alive(...)` | `c.alive(...)` |
| Bare `asyncio.create_task(...)` | `c.app.create_task(...)` |
| `json.dumps` for signals | `stario.json` through `SSE` and `responses.json` |
| A helper function per element | One `render` with the tree written inline |
| Shared `components.py` across features on day one | Each feature keeps its own HTML; merge after it proves stable |
| A second bootstrap for tests | `TestClient(bootstrap)` |
| Python 3.12 idioms | Python 3.14: `type` aliases, PEP 695 generics, `match`, `dataclass(slots=True)` |

## Optional stacks

Each stack is a skill. Install the skill folder under `.agents/skills/` or `.cursor/skills/` in the repo, or read it from https://stario.dev/skills/.

- `stario-feature`: add or remove one screen, step by step.
- `stario-litewriter`: SQLite through `litewriter`. One writer thread, `watch` as the stream notifier instead of `Relay`.
- `stario-tailwind-daisyui`: Tailwind with the standalone CLI and DaisyUI components. No Node.

## Further reading

- https://stario.dev/docs/the-stario-way
- https://stario.dev/docs/explanation/go-to-architecture
- https://stario.dev/docs/how-tos/datastar-sdk
- https://stario.dev/docs/reference/routing
- https://stario.dev/docs/reference/testing
- https://data-star.dev (attribute reference)
