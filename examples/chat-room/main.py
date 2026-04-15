"""
Stario Chat Room — medium-sized layout.

Run with: uv run stario watch main:bootstrap
      or: uv run stario serve main:bootstrap

Bootstrap responsibilities:
- Build shared dependencies used by feature code (here: SQLite ``Database``, ``Relay``).
- Register **one** static mount under ``/static`` so every feature uses the same
  ``url_for("static:…")`` names.
- Register routes on the root ``App`` and **mount** each feature’s ``Router``
  at ``/`` so their patterns are absolute (`/about`, `/subscribe`, …), matching
  how ``app.chat`` is written today.

Feature packages live under ``app/<feature>/`` with ``router.build_router`` and
co-located handlers, views, models, etc.
"""

from pathlib import Path

from app.about.router import build_router as build_about_router
from app.chat.db import create_database
from app.chat.handlers import home
from app.chat.router import build_router as build_chat_router

from stario import App, Relay, Span
from stario.http.staticassets import StaticAssets


async def bootstrap(app: App, span: Span) -> None:
    is_dev = True

    db = create_database(is_dev=is_dev)
    relay: Relay[str] = Relay()

    static_dir = Path(__file__).parent / "app" / "static"
    static_dir_display = (
        static_dir.relative_to(Path.cwd())
        if static_dir.is_relative_to(Path.cwd())
        else static_dir
    )
    span.attrs(
        {
            "chat_room.is_dev": is_dev,
            "chat_room.static_dir": str(static_dir_display),
        }
    )
    app.mount("/static", StaticAssets(static_dir, name="static"))

    app.get("/", home, name="home")
    app.mount("/", build_about_router())
    app.mount("/", build_chat_router(db, relay))
