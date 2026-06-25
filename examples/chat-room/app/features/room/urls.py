"""Room URLs — `ROOMS` is the collection; paths below are per `{room_id}`."""

from stario.routing import UrlPath

ROOMS = UrlPath("/rooms")
ROOM = ROOMS / "{room_id}"
SUBSCRIBE = ROOM / "subscribe"
SEND = ROOM / "send"
TYPING = ROOM / "typing"
