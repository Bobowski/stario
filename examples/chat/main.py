"""
Stario Chat example.

Run with: uv run stario watch main:bootstrap
      or: uv run stario serve main:bootstrap
"""

from pathlib import Path

from app.db import create_database
from app.handlers import home, send_message, subscribe, typing

from stario import Relay, Span, Stario


async def bootstrap(app: Stario, span: Span) -> None:
    # Keep the example simple for now; always use the development database mode.
    is_dev = True

    # Create database - in-memory for dev, file-based for prod
    db = create_database(is_dev=is_dev)

    # Relay for pub/sub between SSE connections
    relay: Relay[str] = Relay()

    # Static files - note: path is relative to this file's location
    static_dir = Path(__file__).parent / "app" / "static"
    static_dir_display = (
        static_dir.relative_to(Path.cwd()) if static_dir.is_relative_to(Path.cwd()) else static_dir
    )
    span.attrs(
        {
            "chat.is_dev": is_dev,
            "chat.static_dir": str(static_dir_display),
        }
    )
    app.assets("/static", static_dir, name="static")

    # Routes - closures inject db/relay where needed
    app.get("/", home, name="home")
    app.get("/subscribe", subscribe(db, relay), name="subscribe")
    app.post("/send", send_message(db, relay), name="send")
    app.post("/typing", typing(db, relay), name="typing")
