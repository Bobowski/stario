"""Room routes — `ROOMS` is the collection; paths below are per `{room_id}`."""

from stario import Route

ROOMS = "/rooms"
ROOM_PATH = ROOMS + "/{room_id}"
ROOM = Route("GET", ROOM_PATH)
SUBSCRIBE = Route("GET", ROOM_PATH + "/subscribe")
SEND = Route("POST", ROOM_PATH + "/send")
TYPING = Route("POST", ROOM_PATH + "/typing")
CREATE = Route("POST", ROOMS)
DELETE = Route("DELETE", ROOM_PATH)
