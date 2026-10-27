"""
Room page — one room URL, SSE stream, and POST commands.

Queries: show, live
Commands: send_message, typing
"""

import time
import uuid
from dataclasses import dataclass

from app.common.identity import identity_for_page
from app.db import Database
from app.features.lobby.urls import LOBBY
from stario import App, Context, Relay, Writer
from stario.datastar import SSE

from . import data, subjects
from .models import Message, Room, User
from .signals import read_chat_signals
from .urls import LIVE, ROOM, SEND, TYPING
from .views import ROOM_LIVE_SELECTOR, room_live_view, room_view

MAX_MESSAGE_LENGTH = 2000


def room_from_route(c: Context, db: Database) -> Room | None:
    room_id = c.match.params.get("room_id", "")
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


@dataclass(frozen=True, slots=True)
class RoomPage:
    db: Database
    relay: Relay[dict]

    def register(self, app: App) -> None:
        app.add(ROOM, self.show)
        app.add(LIVE, self.live)
        app.add(SEND, self.send_message)
        app.add(TYPING, self.typing)

    async def show(self, c: Context, w: Writer) -> None:
        """GET /rooms/{room_id} — mint identity and render the room with history."""
        room = room_from_route(c, self.db)
        if room is None:
            w.redirect(LOBBY.href())
            return

        identity = identity_for_page(c)
        c.span.attrs(
            {
                "user_id": identity.user_id,
                "username": identity.username,
                "room_id": room.id,
            }
        )

        w.html(
            room_view(
                room,
                identity.user_id,
                identity.username,
                identity.color,
                messages=data.list_messages(self.db, room.id),
                users=data.list_users(self.db, room.id),
            ),
        )

    async def live(self, c: Context, w: Writer) -> None:
        """GET /rooms/{room_id}/live — SSE patches for this room.

        `c.alive` ends the loop on client disconnect or server shutdown, so
        the presence cleanup below always runs. If the room is deleted
        mid-stream we navigate the client back to the lobby over SSE.
        """
        room = room_from_route(c, self.db)
        if room is None:
            SSE(w).navigate(LOBBY.href())
            return

        signals = await read_chat_signals(c)
        if not signals.user_id:
            # Reload the room page, which mints a fresh identity.
            SSE(w).navigate(ROOM.href(room_id=room.id))
            return

        user = User(
            id=signals.user_id,
            room_id=room.id,
            username=signals.username,
            color=signals.color,
        )

        # Subscribe first so this connection's queue exists before we publish
        # presence (avoids a gap where join could be dropped for this client).
        async with self.relay.subscribe(
            subjects.room_events(room.id), latest=True
        ) as events:
            sse = SSE(w)
            data.add_user(self.db, user)
            self.relay.publish(subjects.presence(room.id), {})
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
                db=self.db,
            )

            async for subject, _ in c.alive(events):
                c.span.event("relay", {"subject": subject})
                if data.get_room(self.db, room.id) is None:
                    sse.navigate(LOBBY.href())
                    break
                patch_room(
                    sse,
                    room=room,
                    user_id=signals.user_id,
                    username=signals.username,
                    color=signals.color,
                    db=self.db,
                )

        # Loop ended — drop presence, then fan out leave to other subscribers.
        data.remove_user(self.db, signals.user_id, room.id)
        self.relay.publish(subjects.presence(room.id), {})
        c.span.event(
            "User disconnected",
            {"user_id": signals.user_id, "room_id": room.id},
        )

    async def send_message(self, c: Context, w: Writer) -> None:
        """POST /rooms/{room_id}/send — 204 first, then store and publish."""
        room = room_from_route(c, self.db)
        if room is None:
            SSE(w).navigate(LOBBY.href())
            return

        signals = await read_chat_signals(c)
        if not signals.user_id or not data.user_exists(
            self.db, signals.user_id, room.id
        ):
            SSE(w).navigate(ROOM.href(room_id=room.id))
            return

        text = signals.message.strip()[:MAX_MESSAGE_LENGTH]
        if not text:
            w.empty()
            return

        w.empty()

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
        data.add_message(self.db, msg)
        data.set_user_typing(self.db, signals.user_id, room.id, False)
        c.span.event(
            "Message sent",
            {"user_id": signals.user_id, "room_id": room.id, "text": text[:50]},
        )

        self.relay.publish(subjects.message(room.id), {})

    async def typing(self, c: Context, w: Writer) -> None:
        """POST /rooms/{room_id}/typing — typing indicator."""
        room = room_from_route(c, self.db)
        if room is None:
            w.empty()
            return

        signals = await read_chat_signals(c)
        if not signals.user_id or not data.user_exists(
            self.db, signals.user_id, room.id
        ):
            w.empty()
            return

        w.empty()

        is_typing = bool(signals.message.strip())
        if data.set_user_typing(self.db, signals.user_id, room.id, is_typing):
            self.relay.publish(subjects.typing(room.id), {})
