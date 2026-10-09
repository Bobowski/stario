"""
Room feature handlers — one room URL, SSE stream, and POST commands.

Queries: show, live
Commands: send, typing
"""

import time
import uuid

from app.common.identity import identity_for_page
from app.db import Database
from app.features.lobby.urls import LOBBY
from stario import App, Context, HttpException, Relay, Writer, responses
from stario.datastar import SSE, read_signals

from . import data, subjects
from .models import Message, User
from .urls import ROOM, SEND, SUBSCRIBE, TYPING
from .views import render

MAX_MESSAGE_LENGTH = 2000


class Room:
    def __init__(self, db: Database, relay: Relay[str]) -> None:
        self.db = db
        self.relay = relay

    async def show(self, c: Context, w: Writer) -> None:
        """GET /rooms/{room_id} — mint identity and render the room with history."""
        room = data.get_room(self.db, c.match.params["room_id"])
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
            render(
                room,
                identity.user_id,
                identity.username,
                identity.color,
                messages=data.list_messages(self.db, room.id),
                users=data.list_users(self.db, room.id),
            ),
        )

    async def live(self, c: Context, w: Writer) -> None:
        """GET /rooms/{room_id}/subscribe — SSE repaints this room.

        `c.alive` ends the loop on client disconnect or server shutdown, so the
        presence cleanup below always runs. If the room is deleted mid-stream we
        navigate the client back to the lobby over SSE.
        """
        room = data.get_room(self.db, c.match.params["room_id"])
        if room is None:
            SSE(w).navigate(LOBBY.href())
            return

        signals = await read_signals(c.req)
        user_id = str(signals.get("user_id", ""))
        if not user_id:
            raise HttpException(400, "Missing identity signals")
        username = str(signals.get("username", ""))
        color = str(signals.get("color", ""))

        # Subscribe first so this connection's queue exists before we publish
        # presence (avoids a gap where join could be dropped for this client).
        async with self.relay.subscribe(subjects.room_events(room.id)) as events:
            sse = SSE(w)
            data.add_user(
                self.db,
                User(id=user_id, room_id=room.id, username=username, color=color),
            )
            self.relay.publish(subjects.presence(room.id), "join")
            c.span.event(
                "User connected",
                {"user_id": user_id, "username": username, "room_id": room.id},
            )

            sse.patch_elements(
                render(
                    room,
                    user_id,
                    username,
                    color,
                    messages=data.list_messages(self.db, room.id),
                    users=data.list_users(self.db, room.id),
                )
            )

            async for subject, _ in c.alive(events):
                c.span.event("relay", {"subject": subject})
                if data.get_room(self.db, room.id) is None:
                    sse.navigate(LOBBY.href())
                    break
                sse.patch_elements(
                    render(
                        room,
                        user_id,
                        username,
                        color,
                        messages=data.list_messages(self.db, room.id),
                        users=data.list_users(self.db, room.id),
                    )
                )

        # Loop ended — drop presence, then fan out leave to other subscribers.
        data.remove_user(self.db, user_id, room.id)
        self.relay.publish(subjects.presence(room.id), "leave")
        c.span.event(
            "User disconnected",
            {"user_id": user_id, "room_id": room.id},
        )

    async def send(self, c: Context, w: Writer) -> None:
        """POST /rooms/{room_id}/send — 204 first, then store and publish."""
        room = data.get_room(self.db, c.match.params["room_id"])
        if room is None:
            raise HttpException(404, "Room not found")

        signals = await read_signals(c.req)
        user_id = str(signals.get("user_id", ""))
        if not user_id or not data.user_exists(self.db, user_id, room.id):
            raise HttpException(403, "Join the room first")

        text = str(signals.get("message", "")).strip()[:MAX_MESSAGE_LENGTH]
        if not text:
            responses.empty(w)
            return

        responses.empty(w)

        c.span.attrs({"user_id": user_id, "room_id": room.id})

        msg = Message(
            id=str(uuid.uuid4())[:8],
            room_id=room.id,
            user_id=user_id,
            username=str(signals.get("username", "")),
            color=str(signals.get("color", "")),
            text=text,
            timestamp=time.time(),
        )
        data.add_message(self.db, msg)
        data.set_user_typing(self.db, user_id, room.id, False)
        c.span.event(
            "Message sent",
            {"user_id": user_id, "room_id": room.id, "text": text[:50]},
        )

        self.relay.publish(subjects.message(room.id), "new")

    async def typing(self, c: Context, w: Writer) -> None:
        """POST /rooms/{room_id}/typing — typing indicator."""
        room = data.get_room(self.db, c.match.params["room_id"])
        if room is None:
            raise HttpException(404, "Room not found")

        signals = await read_signals(c.req)
        user_id = str(signals.get("user_id", ""))
        if not user_id or not data.user_exists(self.db, user_id, room.id):
            raise HttpException(403, "Join the room first")

        responses.empty(w)

        is_typing = bool(str(signals.get("message", "")).strip())
        if data.set_user_typing(self.db, user_id, room.id, is_typing):
            self.relay.publish(subjects.typing(room.id), "changed")

    def register(self, app: App) -> None:
        app.add(ROOM, self.show)
        app.add(SUBSCRIBE, self.live)
        app.add(SEND, self.send)
        app.add(TYPING, self.typing)
