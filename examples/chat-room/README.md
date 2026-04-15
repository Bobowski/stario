# Chat room

## How to run

From this directory (after dependencies are installed):

```bash
uv sync
uv run stario serve main:bootstrap
```

`stario` is declared like any published app (``stario init`` runs ``uv add 'stario>=N,<N+1'`` for the CLI’s major, e.g. ``stario>=3,<4``; **no** ``tool.uv.sources`` path), so a copy or a CLI-fetched tree only needs an index that can resolve the **stario** distribution—not the monorepo layout.

**In the stario repo** (this example under ``shared/stario/examples/``), prefer ``uv run --with-editable ../.. …`` so dependency resolution stays portable—no ``tool.uv.sources`` path in this file—while commands still use your checkout.

During development, reload on file changes:

```bash
uv run stario watch main:bootstrap
```

Run the example test suite (from this directory so `main` resolves):

```bash
uv run pytest
```

If you use the **published** ``stario`` package from an index, that is enough once the release includes HTTP ``stario.testing.TestClient``.

When this example lives **inside** the stario repo, the index build may lag behind the tree you have on disk. Then point uv at the local framework without changing ``pyproject.toml`` (same mechanism ``stario init`` avoids path mounts for fetched copies):

```bash
uv run --with-editable ../.. pytest
```

Tests use an **async** ``client`` fixture (see ``tests/conftest.py``) that does ``async with TestClient(main.bootstrap)``; pytest-asyncio tears it down after each test. Use ``await client.get(...)`` in tests. ``client.app`` is the wired app (e.g. for ``url_for``).

## What this app is doing

- **HTTP:** `main.bootstrap` wires an `App` instance: one static file mount, a home page route, and **mounted routers** for each feature (`app.chat`, `app.about`). Mounting merges route tables so named routes and `url_for` stay global.

- **Chat feature:** `app.chat` holds handlers, HTML views, SQLite access, domain models, and `router.build_router(db, relay)`. Handlers that need storage or pub/sub are **factories** (`subscribe(db, relay)`) so dependencies are passed from bootstrap, not read from globals.

- **Realtime:** Browsers open an SSE stream; the server pushes HTML fragments (Datastar) when state changes. **Relay** fans out events on dotted subjects (`chat.message`, `chat.presence`, …); every subscriber matching `chat.*` refreshes its client.

- **About feature:** `app.about` is a minimal second package: its `router` registers `GET /about` and is merged with `app.mount("/", …)` alongside chat, sharing the same `/static` mount. More areas add their own paths the same way without duplicating static setup.
