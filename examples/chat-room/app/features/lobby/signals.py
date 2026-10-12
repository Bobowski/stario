"""Lobby page signals — create-room dialog only."""

from dataclasses import dataclass

from stario import Context
from stario.datastar import read_signals


@dataclass
class LobbySignals:
    room_title: str = ""
    room_description: str = ""


async def read_lobby_signals(c: Context) -> LobbySignals:
    payload = await read_signals(c.req)
    return LobbySignals(
        room_title=str(payload.get("room_title", "")),
        room_description=str(payload.get("room_description", "")),
    )
