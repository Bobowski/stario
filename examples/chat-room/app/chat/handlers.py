"""
Stario Chat — request handlers

Each handler is ``async def handler(c: Context, w: Writer)``. ``c`` holds the
request and trace; helper modules build normal responses and Datastar SSE events
on top of the writer's transport primitives.

Dependencies (``db``, ``relay``) are not global. **Handler factories** close over
them: ``subscribe(db, relay)`` returns the real handler. Bootstrap builds ``db``
and ``relay`` once, passes them into ``app.chat.router.build_router``, which wires
factories to paths. The same pattern works for an HTTP client, config object, or
anything else the feature needs.
"""

import time
import uuid
from dataclasses import dataclass

import stario.responses as responses
from stario import Context, Relay, Writer
from stario import datastar as ds

from .db import Database
from .models import Message, User, generate_color, generate_username
from .relay_topics import (
    CHAT_MESSAGE,
    CHAT_PRESENCE,
    CHAT_SUBSCRIBE_PATTERN,
    CHAT_TYPING,
)
from .views import chat_view


@dataclass
class ChatSignals:
    """
    Shape of Datastar signals for this app.

    The framework now returns plain JSON-shaped data; the app owns turning it into
    this dataclass.
    """

    user_id: str = ""
    username: str = ""
    color: str = ""
    message: str = ""


async def read_chat_signals(c: Context) -> ChatSignals:
    payload = await ds.read_signals(c.req)
    return ChatSignals(
        user_id=str(payload.get("user_id", "")),
        username=str(payload.get("username", "")),
        color=str(payload.get("color", "")),
        message=str(payload.get("message", "")),
    )


async def home(c: Context, w: Writer) -> None:
    """
    Full document for the first paint. The SSE handler later patches the same
    ``chat_view`` so the live DOM stays aligned with ``db``.
    """
    user_id = str(uuid.uuid4())[:8]
    username = generate_username()
    color = generate_color()

    responses.html(
        w,
        chat_view(
            c.app,
            user_id,
            username,
            color,
            messages=[],
            users={},
        )
    )


def subscribe(db: Database, relay: Relay[str]):
    """``router.get(..., subscribe(db, relay))`` — captures shared deps."""

    async def handler(c: Context, w: Writer) -> None:
        """
        Long-lived SSE handler: **setup**, **while connected**, **cleanup**.

        Same lifecycle as tiles: ``async with relay.subscribe(...)`` first so
        ``publish`` cannot race ahead of this connection's queue, then
        ``async for`` inside ``w.alive(live)``, then teardown before exiting the
        ``async with``.
        """
        signals = await read_chat_signals(c)

        if not signals.user_id:
            responses.redirect(w, c.app.url_for("home"))
            return

        user = User(
            id=signals.user_id,
            username=signals.username,
            color=signals.color,
        )
        db.add_user(user)
        c.span.event(
            "User connected",
            {"user_id": signals.user_id, "username": signals.username},
        )

        # Register relay queue before CHAT_PRESENCE publish so this client cannot miss events.
        async with relay.subscribe(CHAT_SUBSCRIBE_PATTERN) as live:
            # Fan-out: every SSE client subscribed to ``chat.*`` wakes and patches.
            relay.publish(CHAT_PRESENCE, "join")

            # First patch: stream has started; ship current db truth (messages, roster).
            ds.sse.patch_elements(
                w,
                chat_view(
                    c.app,
                    signals.user_id,
                    signals.username,
                    signals.color,
                    messages=db.get_messages(),
                    users=db.get_users(),
                )
            )

            async for subject, _ in w.alive(live):
                c.span.event("relay", {"subject": subject})
                ds.sse.patch_elements(
                    w,
                    chat_view(
                        c.app,
                        signals.user_id,
                        signals.username,
                        signals.color,
                        messages=db.get_messages(),
                        users=db.get_users(),
                    )
                )

            # Disconnect cleanup — not an error path.
            c.span.event("User disconnected", {"user_id": signals.user_id})
            db.remove_user(signals.user_id)
            relay.publish(CHAT_PRESENCE, "leave")

    return handler


def send_message(db: Database, relay: Relay[str]):
    """``router.post(..., send_message(db, relay))``."""

    async def handler(c: Context, w: Writer) -> None:
        signals = await read_chat_signals(c)

        if not signals.user_id or not db.user_exists(signals.user_id):
            responses.redirect(w, c.app.url_for("home"))
            return

        text = signals.message.strip()
        if not text:
            responses.empty(w, 204)
            return

        msg = Message(
            id=str(uuid.uuid4())[:8],
            user_id=signals.user_id,
            username=signals.username,
            color=signals.color,
            text=text,
            timestamp=time.time(),
        )
        db.add_message(msg)
        db.set_user_typing(signals.user_id, False)

        c.span.event(
            "Message sent",
            {"user_id": signals.user_id, "text": text[:50]},
        )

        responses.empty(w, 204)
        relay.publish(CHAT_MESSAGE, "new")

    return handler


def typing(db: Database, relay: Relay[str]):
    """``router.post(..., typing(db, relay))``."""

    async def handler(c: Context, w: Writer) -> None:
        signals = await read_chat_signals(c)

        if not signals.user_id or not db.user_exists(signals.user_id):
            responses.empty(w, 204)
            return

        is_typing = bool(signals.message.strip())

        if db.set_user_typing(signals.user_id, is_typing):
            relay.publish(CHAT_TYPING, "changed")

        responses.empty(w, 204)

    return handler
