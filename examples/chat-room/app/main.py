"""
Stario Chat Room — multi-file example (larger-app layout).

Run from the project root:

  uv run stario watch app.main:bootstrap
  uv run stario serve app.main:bootstrap

Start here, then open each layer:

  app/config.py         env-first Config, read once below
  app/assets.py         fingerprinted CSS / Datastar
  app/db.py             thin SQLite core (connection + transactions)
  app/common/           baked HTML shell, demo identity
  app/features/lobby/   GET / — room picker; POST/DELETE /rooms…
  app/features/room/    /rooms/{room_id}/… — chat, SSE, commands

Per feature the pattern is the same:

  urls.py      UrlPath constants (room/urls.py owns /rooms paths)
  models.py    domain dataclasses (room feature owns Room, Message, User)
  data.py      SCHEMA + query functions against the shared Database
  subjects.py  relay subject helpers (room.{id}.message, .presence, …)
  signals.py   Datastar signal shape for this page
  views.py     pure HTML (common.shell.page wraps body content)
  handlers.py  handler factories + register_* at the bottom

Files are optional per feature — the lobby is UI over the room domain, so
it has no models.py or data.py of its own.

Layout trees and conventions: examples/chat-room/README.md and
https://stario.dev/docs/how-tos/structuring-apps
"""

from app.assets import ASSETS
from app.config import Config
from app.db import Database
from app.features.lobby.handlers import register_lobby
from app.features.room import data as room_data
from app.features.room.handlers import register_room
from stario import App, Relay, Span, StaticAssets


async def bootstrap(app: App, span: Span):
    config = Config.from_env()

    db = Database(config.db_path)
    db.apply_schema(room_data.SCHEMA)

    relay = Relay()

    span.attrs(
        {
            "chat.db_path": config.db_path,
            "chat.room_count": len(room_data.list_rooms(db)),
            "chat.static_dir": str(ASSETS.directory),
        }
    )

    with span.step("static_assets") as s:
        static = StaticAssets(ASSETS)
        s.attrs(static.stats)
    static.register(app)

    register_lobby(app, db, relay)
    register_room(app, db, relay)
    yield
