# Chat room

Multi-room realtime chat — the **larger-app** counterpart to the single-file **tiles** example, and the reference layout for **Stario + SQLite + Datastar** apps.

Rooms live in SQLite — the lobby starts empty. Create or delete from the lobby — **New room** opens a dialog; each card has a **×** delete button.

## Run

```bash
git clone https://github.com/bobowski/stario.git
cd stario/examples/chat-room
uv sync
uv run stario watch app.main:bootstrap
```

Open http://127.0.0.1:8000 — create a room, then chat in two tabs to see live updates.

## Tests

```bash
uv run pytest
```

Tests use `TestClient(app.main.bootstrap)` — same bootstrap the CLI loads. One test file per feature (`test_lobby.py`, `test_room.py`).

## Layout

```text
app/
  main.py           bootstrap (composition root) — start here
  config.py         env-first Config, read once in bootstrap
  assets.py         AssetManifest + fingerprinted URLs
  db.py             thin SQLite core (connection + transactions)
  common/           page shell, demo identity — cross-feature, no owner
  features/
    lobby/          GET / ; POST/DELETE reuse paths from room/urls.py
    room/           owns the room domain: /rooms… URLs, models, data, chat + SSE
  static/           CSS, vendored Datastar
tests/
pyproject.toml
```

Each feature follows the same shape — every file optional:

| File | Role |
|------|------|
| `urls.py` | `UrlPath` constants (`room/urls.py` defines `ROOMS` and `ROOM / "send"` etc.) |
| `models.py` | domain dataclasses (`room/models.py` owns `Room`, `Message`, `User`) |
| `data.py` | `SCHEMA` DDL + query functions taking the shared `Database` |
| `subjects.py` | relay subject helpers — no typo-prone f-strings at call sites |
| `signals.py` | Datastar signal dataclass + `read_*_signals` for this page |
| `views.py` | HTML trees (`common.shell.page` wraps body content) |
| `handlers.py` | handler factories + `register_*` at the bottom |

`app/main.py` reads `Config.from_env()`, builds shared deps once (`db`, `relay`), applies each feature's `SCHEMA`, registers static assets, then calls each `register_*`.

**Who owns the data:** the room feature owns the whole room domain (tables, models, queries). The lobby is UI over that domain — it imports `room.data` and `room.urls` rather than duplicating them. Domain imports flow one way (lobby consumes room's `data`/`models`, never the reverse); `urls.py` modules are leaves, so any feature may import another's URLs for links and redirects (room redirects to `LOBBY`). `app/db.py` never grows when you add a feature.

## Request flow (CQRS-shaped)

| Route | Job |
|-------|-----|
| `GET /` | Lobby — list rooms and online counts |
| `GET /subscribe` | Long-lived SSE — patch lobby on presence and room list changes |
| `POST /rooms` | Create a room (dialog signals) |
| `DELETE /rooms/{id}` | Delete a room and its messages/presence |
| `GET /rooms/{id}` | First paint for a room (mint demo user) |
| `GET /rooms/{id}/subscribe` | Long-lived SSE — patch HTML on relay events |
| `POST /rooms/{id}/send` | Store message — 204, update via SSE |
| `POST /rooms/{id}/typing` | Typing flag — 204, update via SSE |

Relay subjects are per room (`room.{id}.presence`, `room.{id}.message`, …), built by `room/subjects.py` and subscribed with `room.{id}.*`. The lobby opens `GET /subscribe` and listens on `room.*` and `lobby.*` so online counts and the room list update when users join or leave, or when rooms are created or deleted. When a room is deleted mid-stream, the room subscribe handler calls `SSE(w).navigate(LOBBY.href())` so live tabs return to the lobby without a full page reload.

## Configuration

| Variable | Default | Meaning |
|----------|---------|---------|
| `CHAT_DB_PATH` | `:memory:` | SQLite file path; in-memory keeps dev zero-setup |
