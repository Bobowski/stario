"""Lobby feature — room list, create, delete."""

import json

import pytest

from app.main import bootstrap
from stario.testing import TestClient


@pytest.mark.asyncio
async def test_lobby_starts_empty(client):
    r = await client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")
    assert "no rooms yet" in r.text.lower()


@pytest.mark.asyncio
async def test_create_room(client):
    r = await client.post(
        "/rooms",
        json={"room_title": "Design Chat", "room_description": "UI talk"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers.get("location", "").endswith("/rooms/design-chat")
    room = await client.get("/rooms/design-chat")
    assert room.status_code == 200
    assert "design chat" in room.text.lower()


@pytest.mark.asyncio
async def test_delete_room(client):
    await client.post(
        "/rooms",
        json={"room_title": "Temp", "room_description": "Gone soon"},
    )
    r = await client.delete("/rooms/temp", follow_redirects=False)
    assert r.status_code == 303
    lobby = await client.get("/")
    assert "temp" not in lobby.text.lower()


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


@pytest.mark.asyncio
async def test_each_tab_gets_distinct_identity():
    async with TestClient(bootstrap) as tab_a, TestClient(bootstrap) as tab_b:
        id_a = _signal_value((await tab_a.get("/")).text, "user_id")
        id_b = _signal_value((await tab_b.get("/")).text, "user_id")
    assert id_a
    assert id_b
    assert id_a != id_b


@pytest.mark.asyncio
async def test_lobby_subscribe(client):
    lobby = await client.get("/")
    user_id = _signal_value(lobby.text, "user_id")
    assert user_id
    signals = json.dumps({"user_id": user_id, "username": "HappyFox", "color": "#e74c3c"})
    saw_patch = False
    async with client.stream(
        "GET",
        "/subscribe",
        params={"datastar": signals},
        headers={"Accept-Encoding": "identity"},
    ) as r:
        assert r.status_code == 200
        assert "text/event-stream" in r.headers.get("content-type", "")
        async for event in r.iter_events():
            if event.get("event") == "datastar-patch-elements":
                saw_patch = True
                break
    assert saw_patch
