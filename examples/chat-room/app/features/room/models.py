"""Room domain types — owned by this feature, imported by others (lobby)."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Room:
    id: str
    title: str
    description: str


@dataclass(frozen=True, slots=True)
class Message:
    id: str
    room_id: str
    user_id: str
    username: str
    color: str
    text: str
    timestamp: float


@dataclass(frozen=True, slots=True)
class User:
    id: str
    room_id: str
    username: str
    color: str
    typing: bool = False
