"""
Room feature handlers — one room URL, SSE stream, and POST commands.

Queries: show_room, subscribe
Commands: send_message, typing
"""

import time
import uuid

import stario.responses as responses
from app.common.identity import identity_for_page
from app.db import Database
from app.features.lobby.urls import LOBBY
from stario import App, Context, Relay, Writer
from stario.datastar import SSE

from . import data, subjects
from .models import Message, Room, User
from .signals import read_chat_signals
from .urls import ROOM, SEND, SUBSCRIBE, TYPING
from .views import ROOM_LIVE_SELECTOR, room_live_view, room_view

MAX_MESSAGE_LENGTH = 2000


def room_from_route(c: Context, db: Database) -> Room | None:
    room_id = c.route.params.get("room_id", "")
    return data.get_room(db, room_id) if room_id else None


def patch_room(
    sse: SSE,
    *,
    room: Room,
    user_id: str,
    username: str,
    color: str,
    db: Database,
) -> None:
    sse.patch_elements(
        room_live_view(
            room,
            user_id,
            messages=data.list_messages(db, room.id),
            users=data.list_users(db, room.id),
        ),
        selector=ROOM_LIVE_SELECTOR,
        mode="outer",
    )


def show_room(db: Database):
    """GET /rooms/{room_id} — mint identity and render the room with history."""

    async def handler(c: Context, w: Writer) -> None:
        room = room_from_route(c, db)
        if room is None:
            responses.redirect(w, LOBBY.href())
            return

        identity = identity_for_page(c)
        c.span.attrs(
            {
                "user_id": identity.user_id,
                "username": identity.username,
                "room_id": room.id,
            }
        )

        responses.html(
            w,
            room_view(
                room,
                identity.user_id,
                identity.username,
                identity.color,
                messages=data.list_messages(db, room.id),
                users=data.list_users(db, room.id),
            ),
        )

    return handler


def subscribe(db: Database, relay: Relay[str]):
    """GET /rooms/{room_id}/subscribe — SSE patches for this room.

    `c.alive` ends the loop on client disconnect or server shutdown, so the
    presence cleanup below always runs. If the room is deleted mid-stream we
    navigate the client back to the lobby over SSE.
    """

    async def handler(c: Context, w: Writer) -> None:
        room = room_from_route(c, db)
        if room is None:
            responses.redirect(w, LOBBY.href())
            return

        signals = await read_chat_signals(c)
        if not signals.user_id:
            responses.redirect(w, ROOM.href(room_id=room.id))
            return

        user = User(
            id=signals.user_id,
            room_id=room.id,
            username=signals.username,
            color=signals.color,
        )

        # Subscribe first so this connection's queue exists before we publish
        # presence (avoids a gap where join could be dropped for this client).
        async with relay.subscribe(subjects.room_events(room.id)) as live:
            sse = SSE(w)
            data.add_user(db, user)
            relay.publish(subjects.presence(room.id), "join")
            c.span.event(
                "User connected",
                {
                    "user_id": signals.user_id,
                    "username": signals.username,
                    "room_id": room.id,
                },
            )

            patch_room(
                sse,
                room=room,
                user_id=signals.user_id,
                username=signals.username,
                color=signals.color,
                db=db,
            )

            async for subject, _ in c.alive(live):
                c.span.event("relay", {"subject": subject})
                if data.get_room(db, room.id) is None:
                    sse.navigate(LOBBY.href())
                    break
                patch_room(
                    sse,
                    room=room,
                    user_id=signals.user_id,
                    username=signals.username,
                    color=signals.color,
                    db=db,
                )

        # Loop ended — drop presence, then fan out leave to other subscribers.
        data.remove_user(db, signals.user_id, room.id)
        relay.publish(subjects.presence(room.id), "leave")
        c.span.event(
            "User disconnected",
            {"user_id": signals.user_id, "room_id": room.id},
        )

    return handler


def send_message(db: Database, relay: Relay[str]):
    """POST /rooms/{room_id}/send — store a message, then 204."""

    async def handler(c: Context, w: Writer) -> None:
        room = room_from_route(c, db)
        if room is None:
            responses.redirect(w, LOBBY.href(), 303)
            return

        signals = await read_chat_signals(c)
        if not signals.user_id or not data.user_exists(db, signals.user_id, room.id):
            responses.redirect(w, ROOM.href(room_id=room.id), 303)
            return

        text = signals.message.strip()[:MAX_MESSAGE_LENGTH]
        if not text:
            responses.empty(w, 204)
            return

        c.span.attrs({"user_id": signals.user_id, "room_id": room.id})

        msg = Message(
            id=str(uuid.uuid4())[:8],
            room_id=room.id,
            user_id=signals.user_id,
            username=signals.username,
            color=signals.color,
            text=text,
            timestamp=time.time(),
        )
        data.add_message(db, msg)
        data.set_user_typing(db, signals.user_id, room.id, False)
        c.span.event(
            "Message sent",
            {"user_id": signals.user_id, "room_id": room.id, "text": text[:50]},
        )

        relay.publish(subjects.message(room.id), "new")
        responses.empty(w, 204)

    return handler


def typing(db: Database, relay: Relay[str]):
    """POST /rooms/{room_id}/typing — typing indicator."""

    async def handler(c: Context, w: Writer) -> None:
        room = room_from_route(c, db)
        if room is None:
            responses.empty(w, 204)
            return

        signals = await read_chat_signals(c)
        if not signals.user_id or not data.user_exists(db, signals.user_id, room.id):
            responses.empty(w, 204)
            return

        is_typing = bool(signals.message.strip())
        if data.set_user_typing(db, signals.user_id, room.id, is_typing):
            relay.publish(subjects.typing(room.id), "changed")

        responses.empty(w, 204)

    return handler


def register_room(app: App, db: Database, relay: Relay[str]) -> None:
    app.get(ROOM, show_room(db))
    app.get(SUBSCRIBE, subscribe(db, relay))
    app.post(SEND, send_message(db, relay))
    app.post(TYPING, typing(db, relay))
