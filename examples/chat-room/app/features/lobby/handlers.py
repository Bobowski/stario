"""
Lobby handlers — room list landing page.

The lobby is UI over the room domain: it imports queries from
`app.features.room.data` instead of owning tables of its own.
"""

from app.common.identity import identity_for_page, resolve_identity
from app.db import Database
from app.features.room import data as room_data
from app.features.room import subjects as room_subjects
from app.features.room.urls import CREATE, DELETE, ROOM
from stario import App, Context, HttpException, Relay, Writer, responses
from stario.datastar import SSE, read_signals

from .urls import LOBBY, SUBSCRIBE
from .views import render

MAX_TITLE_LENGTH = 60
MAX_DESCRIPTION_LENGTH = 200

ROOMS_CHANGED = "lobby.rooms"
_LOBBY_REFRESH_SUFFIXES = (".presence", ".deleted")


class Lobby:
    def __init__(self, db: Database, relay: Relay[str]) -> None:
        self.db = db
        self.relay = relay

    async def show(self, c: Context, w: Writer) -> None:
        """GET / — mint or restore visitor identity and list rooms."""
        identity = identity_for_page(c)
        c.span.attrs(
            {
                "user_id": identity.user_id,
                "username": identity.username,
            }
        )
        responses.html(
            w,
            render(
                identity=identity,
                rooms=room_data.list_rooms(self.db),
                online_counts=room_data.count_users_by_room(self.db),
            ),
        )

    async def live(self, c: Context, w: Writer) -> None:
        """GET /subscribe — SSE repaints when rooms or presence change."""
        identity = await resolve_identity(c)
        if identity is None:
            raise HttpException(400, "Missing identity signals")

        async with self.relay.subscribe("room.*", "lobby.*") as events:
            sse = SSE(w)
            sse.patch_elements(
                render(
                    identity=identity,
                    rooms=room_data.list_rooms(self.db),
                    online_counts=room_data.count_users_by_room(self.db),
                )
            )

            async for subject, _ in c.alive(events):
                c.span.event("relay", {"subject": subject})
                if subject.startswith("lobby.") or any(
                    subject.endswith(s) for s in _LOBBY_REFRESH_SUFFIXES
                ):
                    sse.patch_elements(
                        render(
                            identity=identity,
                            rooms=room_data.list_rooms(self.db),
                            online_counts=room_data.count_users_by_room(self.db),
                        )
                    )

    async def create(self, c: Context, w: Writer) -> None:
        """POST /rooms — add a room from lobby dialog signals."""
        signals = await read_signals(c.req)
        title = str(signals.get("room_title", "")).strip()[:MAX_TITLE_LENGTH]
        if not title:
            responses.empty(w)
            return

        description = (str(signals.get("room_description", "")).strip() or title)[
            :MAX_DESCRIPTION_LENGTH
        ]
        room = room_data.add_room(self.db, title=title, description=description)
        c.span.event("Room created", {"room_id": room.id, "title": room.title})
        self.relay.publish(ROOMS_CHANGED, "created")
        SSE(w).navigate(ROOM.href(room_id=room.id))

    async def delete(self, c: Context, w: Writer) -> None:
        """DELETE /rooms/{room_id} — remove room and its chat data.

        Publishes a deleted event so live subscribers get navigated back to the
        lobby instead of chatting into a void.
        """
        room_id = c.match.params["room_id"]
        if room_data.delete_room(self.db, room_id):
            c.span.event("Room deleted", {"room_id": room_id})
            self.relay.publish(room_subjects.deleted(room_id), "deleted")
            self.relay.publish(ROOMS_CHANGED, "deleted")
        SSE(w).navigate(LOBBY.href())

    def register(self, app: App) -> None:
        app.add(LOBBY, self.show)
        app.add(SUBSCRIBE, self.live)
        app.add(CREATE, self.create)
        app.add(DELETE, self.delete)
