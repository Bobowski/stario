"""
Lobby handlers — room list landing page.

The lobby is UI over the room domain: it imports queries from
`app.features.room.data` instead of owning tables of its own.
"""

import stario.responses as responses
from app.common.identity import VisitorIdentity, identity_for_page, resolve_identity
from app.db import Database
from app.features.room import data as room_data
from app.features.room import subjects as room_subjects
from app.features.room.urls import ROOM, ROOMS
from stario import App, Context, Relay, Writer
from stario.datastar import SSE

from . import subjects as lobby_subjects
from .signals import read_lobby_signals
from .urls import LOBBY, SUBSCRIBE
from .views import LOBBY_LIVE_SELECTOR, lobby_live_view, lobby_view

MAX_TITLE_LENGTH = 60
MAX_DESCRIPTION_LENGTH = 200

_LOBBY_REFRESH_SUFFIXES = (".presence", ".deleted")


def patch_lobby(sse: SSE, db: Database, identity: VisitorIdentity) -> None:
    rooms = room_data.list_rooms(db)
    online = room_data.count_users_by_room(db)
    sse.patch_elements(
        lobby_live_view(rooms=rooms, online_counts=online),
        selector=LOBBY_LIVE_SELECTOR,
        mode="outer",
    )


def show_lobby(db: Database):
    """GET / — mint or restore visitor identity and list rooms."""

    async def handler(c: Context, w: Writer) -> None:
        identity = identity_for_page(c)
        c.span.attrs(
            {
                "user_id": identity.user_id,
                "username": identity.username,
            }
        )
        rooms = room_data.list_rooms(db)
        online = room_data.count_users_by_room(db)
        responses.html(
            w,
            lobby_view(rooms=rooms, online_counts=online, identity=identity),
        )

    return handler


def subscribe(db: Database, relay: Relay[str]):
    """GET /subscribe — SSE patches when rooms or presence change."""

    async def handler(c: Context, w: Writer) -> None:
        identity = await resolve_identity(c)
        if identity is None:
            responses.redirect(w, LOBBY.href())
            return

        async with relay.subscribe("room.*", "lobby.*") as live:
            sse = SSE(w)
            patch_lobby(sse, db, identity)

            async for subject, _ in c.alive(live):
                c.span.event("relay", {"subject": subject})
                if subject.startswith("lobby.") or any(subject.endswith(s) for s in _LOBBY_REFRESH_SUFFIXES):
                    patch_lobby(sse, db, identity)

    return handler


def create_room(db: Database, relay: Relay[str]):
    """POST /rooms — add a room from lobby dialog signals."""

    async def handler(c: Context, w: Writer) -> None:
        signals = await read_lobby_signals(c)
        title = signals.room_title.strip()[:MAX_TITLE_LENGTH]
        if not title:
            responses.redirect(w, LOBBY.href(), 303)
            return

        description = (signals.room_description.strip() or title)[
            :MAX_DESCRIPTION_LENGTH
        ]
        room = room_data.add_room(db, title=title, description=description)
        c.span.event("Room created", {"room_id": room.id, "title": room.title})
        relay.publish(lobby_subjects.rooms(), "created")
        responses.redirect(w, ROOM.href(room_id=room.id), 303)

    return handler


def delete_room(db: Database, relay: Relay[str]):
    """DELETE /rooms/{room_id} — remove room and its chat data.

    Publishes a deleted event so live subscribers get navigated back to the
    lobby instead of chatting into a void.
    """

    async def handler(c: Context, w: Writer) -> None:
        room_id = c.route.params.get("room_id", "")
        if room_data.delete_room(db, room_id):
            c.span.event("Room deleted", {"room_id": room_id})
            relay.publish(room_subjects.deleted(room_id), "deleted")
            relay.publish(lobby_subjects.rooms(), "deleted")
        responses.redirect(w, LOBBY.href(), 303)

    return handler


def register_lobby(app: App, db: Database, relay: Relay[str]) -> None:
    app.get(LOBBY, show_lobby(db))
    app.get(SUBSCRIBE, subscribe(db, relay))
    app.post(ROOMS, create_room(db, relay))
    app.delete(ROOM, delete_room(db, relay))
