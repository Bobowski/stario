"""
HTTP route table for the chat feature.

Each feature package (``app.chat``, future ``app.profile``, …) can expose a
``router`` module whose job is to return a configured ``Router``. ``main``
merges those routers at ``/`` (see ``main.bootstrap``) so paths are absolute:
``/subscribe``, ``/send``, ``/about``, etc.

Static assets are usually registered once on the root app in ``main.bootstrap``
(see ``main.py``) so all features share ``/static`` and ``url_for("static:…")``.
This module only wires HTTP handlers.

**Naming:** we use ``router.py`` (not ``routes.py``) because the public value is a
``stario.http.Router`` instance; ``routes`` often reads like “a list of paths” in
other frameworks. The entrypoint is ``build_router`` so imports stay uniform
across features: ``from app.chat.router import build_router``.
"""

from stario import Relay, Router

from .db import Database
from .handlers import send_message, subscribe, typing


def build_router(db: Database, relay: Relay[str]) -> Router:
    """Register chat endpoints; ``main.bootstrap`` passes shared ``db`` and ``relay``."""
    r = Router()
    r.get("/subscribe", subscribe(db, relay), name="subscribe")
    r.post("/send", send_message(db, relay), name="send")
    r.post("/typing", typing(db, relay), name="typing")
    return r
