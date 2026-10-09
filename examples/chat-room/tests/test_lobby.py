"""Lobby feature — the page loads; commands repaint the stream."""

import json

from app.main import bootstrap
from stario.testing import TestClient


async def test_lobby_loads(client):
    r = await client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")
    assert "no rooms yet" in r.text.lower()


async def test_create_room_navigates_to_the_room(client):
    r = await client.post(
        "/rooms",
        json={"room_title": "Design Chat", "room_description": "UI talk"},
    )
    assert r.status_code == 200
    assert "text/event-stream" in r.headers.get("content-type", "")
    assert "window.location" in r.text
    assert "/rooms/design-chat" in r.text

    room = await client.get("/rooms/design-chat")
    assert room.status_code == 200
    assert "design chat" in room.text.lower()


async def test_create_room_with_empty_title_is_a_noop(client):
    r = await client.post("/rooms", json={"room_title": "  "})
    assert r.status_code == 204
    lobby = await client.get("/")
    assert "no rooms yet" in lobby.text.lower()


async def test_delete_room_navigates_to_the_lobby(client):
    await client.post("/rooms", json={"room_title": "Temp"})
    r = await client.delete("/rooms/temp")
    assert r.status_code == 200
    assert "window.location" in r.text
    lobby = await client.get("/")
    assert "temp" not in lobby.text.lower()


async def test_create_repaints_the_lobby_stream(client):
    signals = json.dumps({"user_id": "u1", "username": "HappyFox", "color": "#e74c3c"})
    async with client.stream(
        "GET",
        "/subscribe",
        params={"datastar": signals},
        headers={"accept-encoding": "identity"},
    ) as live:
        events = live.iter_events()
        first = await anext(events)
        assert "no rooms yet" in first["data"].lower()

        r = await client.post("/rooms", json={"room_title": "Design Chat"})
        assert r.status_code == 200

        repaint = await anext(events)
        assert "Design Chat" in repaint["data"]


def _signal_value(html: str, key: str) -> str | None:
    marker = f'"{key}"'
    start = html.find(marker)
    if start < 0:
        return None
    chunk = html[start : start + 120]
    for part in chunk.split(","):
        if part.strip().startswith(f'"{key}"'):
            _, _, value = part.partition(":")
            return value.strip().strip('"').strip("'")
    return None


async def test_each_tab_gets_distinct_identity():
    async with TestClient(bootstrap) as tab_a, TestClient(bootstrap) as tab_b:
        id_a = _signal_value((await tab_a.get("/")).text, "user_id")
        id_b = _signal_value((await tab_b.get("/")).text, "user_id")
    assert id_a
    assert id_b
    assert id_a != id_b
