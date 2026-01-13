"""
Stario Chat - Request Handlers

Handlers are async functions that receive:
- Context (c): request info, signals parsing, tracing/logging
- Writer (w): response methods (html, patch, redirect, empty)

Dependencies (db, relay) are injected via closures where needed.
Functions like subscribe(db, relay) return a handler with deps captured.
"""

import time
import uuid
from dataclasses import dataclass

from stario import Context, Relay, Writer

from .db import Database
from .state import Message, User, generate_color, generate_username
from .views import chat_view


@dataclass
class ChatSignals:
    """
    Schema for signals sent from client.

    Datastar automatically sends signals with every request (@get, @post).
    Using a dataclass lets us parse and validate them with c.signals(ChatSignals).
    """

    user_id: str = ""
    username: str = ""
    color: str = ""
    message: str = ""


async def home(c: Context, w: Writer) -> None:
    """
    Serve the initial chat page.

    Each visitor gets a fresh identity (user_id, username, color).
    The identity is stored in Datastar signals on the client side,
    and sent with every subsequent request.
    """
    user_id = str(uuid.uuid4())[:8]
    username = generate_username()
    color = generate_color()

    # Pass empty collections - user will get real data after subscribing
    w.html(chat_view(user_id, username, color, messages=[], users={}))


def subscribe(db: Database, relay: Relay[str]):
    """
    Factory that returns SSE subscription handler with db and relay injected.

    Usage: app.get("/subscribe", subscribe(db, relay))
    """

    async def handler(c: Context, w: Writer) -> None:
        """
        SSE endpoint for real-time updates.

        1. Client connects (triggered by data.init in the HTML)
        2. We register them in the database
        3. We send initial state via w.patch()
        4. We loop, waiting for relay events and sending patches
        5. When client disconnects, the loop exits and we clean up
        """
        signals = await c.signals(ChatSignals)

        if not signals.user_id:
            w.redirect("/")
            return

        # Add user to database
        user = User(
            id=signals.user_id,
            username=signals.username,
            color=signals.color,
        )
        db.add_user(user)
        c("User connected", {"user_id": signals.user_id, "username": signals.username})

        # Tell everyone that someone joined
        relay.publish("update", "presence")

        # Send current state immediately
        w.patch(
            chat_view(
                signals.user_id,
                signals.username,
                signals.color,
                messages=db.get_messages(),
                users=db.get_users(),
            )
        )

        # Main loop: wait for events, send patches
        async for _, event_type in w.alive(relay.subscribe("update")):
            c("event_type", {"event_type": event_type})
            w.patch(
                chat_view(
                    signals.user_id,
                    signals.username,
                    signals.color,
                    messages=db.get_messages(),
                    users=db.get_users(),
                )
            )

        # Cleanup on disconnect
        c("User disconnected", {"user_id": signals.user_id})
        db.remove_user(signals.user_id)
        relay.publish("update", "presence")

    return handler


def send_message(db: Database, relay: Relay[str]):
    """
    Factory that returns message send handler with db and relay injected.

    Usage: app.post("/send", send_message(db, relay))
    """

    async def handler(c: Context, w: Writer) -> None:
        """Handle new message submission."""
        signals = await c.signals(ChatSignals)

        if not signals.user_id or not db.user_exists(signals.user_id):
            w.redirect("/")
            return

        text = signals.message.strip()
        if not text:
            w.empty(204)
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

        c("Message sent", {"user_id": signals.user_id, "text": text[:50]})

        w.empty(204)
        relay.publish("update", "message")

    return handler


def typing(db: Database, relay: Relay[str]):
    """
    Factory that returns typing indicator handler with db and relay injected.

    Usage: app.post("/typing", typing(db, relay))
    """

    async def handler(c: Context, w: Writer) -> None:
        """Update typing indicator status."""
        signals = await c.signals(ChatSignals)

        if not signals.user_id or not db.user_exists(signals.user_id):
            w.empty(204)
            return

        is_typing = bool(signals.message.strip())

        if db.set_user_typing(signals.user_id, is_typing):
            relay.publish("update", "typing")

        w.empty(204)

    return handler
