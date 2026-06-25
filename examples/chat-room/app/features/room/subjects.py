"""
Relay subjects for room events — defined once, next to the feature that owns
them. A typo'd subject string is a silent no-op; going through these helpers
makes that failure impossible.
"""


def room_events(room_id: str) -> str:
    """Wildcard pattern matching every event in one room."""
    return f"room.{room_id}.*"


def presence(room_id: str) -> str:
    return f"room.{room_id}.presence"


def message(room_id: str) -> str:
    return f"room.{room_id}.message"


def typing(room_id: str) -> str:
    return f"room.{room_id}.typing"


def deleted(room_id: str) -> str:
    return f"room.{room_id}.deleted"
