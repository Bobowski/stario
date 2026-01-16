"""
Stario Chat - Application Entry Point

This file bootstraps the application:
1. Configures tracing (RichTracer for dev, JsonTracer for production)
2. Creates database (in-memory for dev, file-based for production)
3. Registers routes with dependencies injected via closures
4. Starts the server

Run with: uv run main.py
      or: python main.py
"""

import asyncio
import sys
from pathlib import Path

from app.db import create_database
from app.handlers import home, send_message, subscribe, typing

from stario import JsonTracer, Relay, RichTracer, Stario


async def main():
    # Determine environment
    is_dev = "--local" in sys.argv or sys.stdout.isatty()

    if is_dev:
        tracer = RichTracer()
        host = "127.0.0.1"
        port = 8000
        workers = 1
    else:
        tracer = JsonTracer()
        host = "0.0.0.0"
        port = 8000
        workers = 4

    # Create database - in-memory for dev, file-based for prod
    db = create_database(is_dev=is_dev)

    # Relay for pub/sub between SSE connections
    relay: Relay[str] = Relay()

    with tracer:
        app = Stario(tracer)

        # Static files - note: path is relative to this file's location
        app.assets("/static", Path(__file__).parent / "app" / "static")

        # Routes - closures inject db/relay where needed
        app.get("/", home)
        app.get("/subscribe", subscribe(db, relay))
        app.post("/send", send_message(db, relay))
        app.post("/typing", typing(db, relay))

        await app.serve(host=host, port=port, workers=workers)


if __name__ == "__main__":
    asyncio.run(main())
