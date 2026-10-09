---
name: stario-litewriter
description: SQLite in a Stario app through litewriter (one writer thread, group commit, watch). Use when a Stario app needs persistent storage, when the user says SQLite or litewriter, or when a live screen should repaint from a database change instead of Relay.
---

# SQLite with litewriter

`litewriter` is the default store for a Stario app. One SQLite file. One writer thread. Reads from any thread on their own connection. `watch` wakes a stream when a commit changes the rows it reads. It needs CPython 3.14.

```bash
uv add litewriter
```

Source and full reference: https://github.com/Bobowski/litewriter

## Open the file in bootstrap

```python
from litewriter import LiteWriter
from stario import App, Span

from app.config import Config
from app.features.board import data as board_data
from app.features.board.handlers import Board


async def bootstrap(app: App, span: Span):
    config = Config.from_env()
    async with LiteWriter(config.db_path, claim=30) as db:
        db.execute_script(board_data.SCHEMA)
        span.attrs(await ASSETS.attach(app))
        Board(db).register(app)
        yield
```

`async with` claims the file, starts the writer, serves, then drains and closes after `yield`. `claim=30` makes a second process wait 30 seconds, then fail, so two servers never write one file. `execute_script` runs DDL. Every `SCHEMA` statement is `CREATE TABLE IF NOT EXISTS`, so a restart is safe.

There is no `app/db.py`. Pass `db` into each feature class.

## Reads

Reads are plain calls on the loop thread. They do not queue behind the writer.

```python
rows = db.query("SELECT id, color FROM tiles ORDER BY id")
row = db.one("SELECT color FROM tiles WHERE id = :id", id=3)
count = db.value("SELECT count(*) FROM tiles")
```

A query is SQL text or a `Select`. Text is fine. Use a `Select` when a page composes the same query with different filters:

```python
from litewriter import Select, col, param

TILES = Select("id", "color", from_="tiles", order_by=col("id"))
RED = TILES.where(col("color") == param("color"))

rows = db.query(RED, color="#ff0000")
```

`.where()` returns a new statement. The original stays as it was. Values from outside always go in as `:name` parameters.

## Writes

A write is a function of `tx`. The writer runs it on its own thread inside one transaction and commits in a batch. You do not commit.

```python
from litewriter import Insert, Tx, Update


def add_tile(tx: Tx, color: str) -> int:
    return tx.value(Insert("tiles", values={"color": ":color"}, returning="id"), color=color)


def set_color(tx: Tx, tile_id: int, color: str) -> None:
    tx.execute("UPDATE tiles SET color = :color WHERE id = :id", {"color": color, "id": tile_id})
```

In a handler:

```python
tile_id = await db.call(add_tile, "#ff0000")
await db.call(set_color, 3, "#00ff00")
```

`await db.call(fn, ...)` waits for the commit. `db.push(fn, ...)` does not wait; errors go to `on_error`. Use `call` in a command, so a failure becomes a response.

Put the write functions in the feature's `data.py` next to the queries. `tx.value`, `tx.one`, `tx.query`, and `tx.execute` mirror the read calls.

## The stream repaints from the database

`watch` replaces `Relay` when the state lives in the file. The first read is the rows now. Each later read comes after a commit that changed them. Equal rows do not wake.

```python
async def live(self, c: Context, w: Writer) -> None:
    sse = SSE(w)
    async with self.db.watch(TILES) as rows:
        async for tiles in c.alive(rows):
            sse.patch_elements(render([Tile(*row) for row in tiles]))
```

A command then has no publish step. It validates, replies `204`, and calls `db.call`. The commit wakes every open stream that reads those rows.

Keep `Relay` for events that are not rows: presence, typing, a toast. Mix both in one `live` handler only when the screen needs both. Then subscribe to `Relay` and read the rows again inside the loop.

## Tests

Point `Config` at a temporary file. The writer creates the file on start.

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

`Config.from_env()` runs inside `bootstrap`, so each test gets its own file.

## Rules

- One `LiteWriter` per file per process. Open it in `bootstrap`. Pass it down.
- Reads on the loop thread. Writes through `db.call`. Never hold `tx` after the function returns.
- `:name` parameters for every outside value. No f-strings into SQL.
- `IF NOT EXISTS` in every `SCHEMA`. A column added later gets its own `ALTER TABLE` guarded by a check of `pragma_table_info`.
- `watch` for rows. `Relay` for events that are not rows.
