"""Lobby routes."""

from stario import Route, UrlPath

ROOT = UrlPath("/")
LOBBY = Route.get(ROOT)
SUBSCRIBE = Route.get(ROOT / "subscribe")
