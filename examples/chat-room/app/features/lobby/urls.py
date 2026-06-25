"""Lobby URL."""

from stario.routing import UrlPath

LOBBY = UrlPath("/")
SUBSCRIBE = LOBBY / "subscribe"
