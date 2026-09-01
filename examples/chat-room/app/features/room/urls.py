"""Room routes — `ROOMS` is the collection; paths below are per `{room_id}`."""

from stario import Route, UrlPath

ROOMS = UrlPath("/rooms")
ROOM_PATH = ROOMS / "{room_id}"
ROOM = Route.get(ROOM_PATH)
SUBSCRIBE = Route.get(ROOM_PATH / "subscribe")
SEND = Route.post(ROOM_PATH / "send")
TYPING = Route.post(ROOM_PATH / "typing")
CREATE = Route.post(ROOMS)
DELETE = Route.delete(ROOM_PATH)
