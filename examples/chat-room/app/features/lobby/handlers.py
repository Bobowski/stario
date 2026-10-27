"""
Lobby page — room list landing page.

The lobby is UI over the room domain: it imports queries from
`app.features.room.data` instead of owning tables of its own.
"""

from dataclasses import dataclass

from app.common.identity import VisitorIdentity, identity_for_page, resolve_identity
from app.db import Database
from app.features.room import data as room_data
from app.features.room import subjects as room_subjects
from app.features.room.urls import CREATE, DELETE, ROOM
from stario import App, Context, Relay, Writer
from stario.datastar import SSE

from . import subjects as lobby_subjects
from .signals import read_lobby_signals
from .urls import LIVE, LOBBY
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


@dataclass(frozen=True, slots=True)
class LobbyPage:
    db: Database
    relay: Relay[dict]

    def register(self, app: App) -> None:
        app.add(LOBBY, self.show)
        app.add(LIVE, self.live)
        app.add(CREATE, self.create_room)
        app.add(DELETE, self.delete_room)

    async def show(self, c: Context, w: Writer) -> None:
        """GET / — mint or restore visitor identity and list rooms."""
        identity = identity_for_page(c)
        c.span.attrs(
            {
                "user_id": identity.user_id,
                "username": identity.username,
            }
        )
        rooms = room_data.list_rooms(self.db)
        online = room_data.count_users_by_room(self.db)
        w.html(
            lobby_view(rooms=rooms, online_counts=online, identity=identity),
        )

    async def live(self, c: Context, w: Writer) -> None:
        """GET /live — SSE patches when rooms or presence change."""
        identity = await resolve_identity(c)
        if identity is None:
            # Reload the lobby page, which mints a fresh identity.
            SSE(w).navigate(LOBBY.href())
            return

        async with self.relay.subscribe("room.*", "lobby.*", latest=True) as events:
            sse = SSE(w)
            patch_lobby(sse, self.db, identity)

            async for subject, _ in c.alive(events):
                c.span.event("relay", {"subject": subject})
                if subject.startswith("lobby.") or any(
                    subject.endswith(s) for s in _LOBBY_REFRESH_SUFFIXES
                ):
                    patch_lobby(sse, self.db, identity)

    async def create_room(self, c: Context, w: Writer) -> None:
        """POST /rooms — add a room from lobby dialog signals."""
        signals = await read_lobby_signals(c)
        title = signals.room_title.strip()[:MAX_TITLE_LENGTH]
        if not title:
            w.empty()
            return

        description = (signals.room_description.strip() or title)[
            :MAX_DESCRIPTION_LENGTH
        ]
        room = room_data.add_room(self.db, title=title, description=description)
        c.span.event("Room created", {"room_id": room.id, "title": room.title})
        SSE(w).navigate(ROOM.href(room_id=room.id))
        self.relay.publish(lobby_subjects.LOBBY_CHANGED, {})

    async def delete_room(self, c: Context, w: Writer) -> None:
        """DELETE /rooms/{room_id} — remove room and its chat data.

        Publishes a deleted event so live subscribers get navigated back to
        the lobby instead of chatting into a void.
        """
        room_id = c.match.params.get("room_id", "")
        w.empty()
        if room_data.delete_room(self.db, room_id):
            c.span.event("Room deleted", {"room_id": room_id})
            self.relay.publish(room_subjects.deleted(room_id), {})
            self.relay.publish(lobby_subjects.LOBBY_CHANGED, {})
