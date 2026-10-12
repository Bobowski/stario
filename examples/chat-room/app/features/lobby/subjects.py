"""Relay subjects for lobby live updates."""


def rooms() -> str:
    """Room list changed (created or deleted)."""
    return "lobby.rooms"
