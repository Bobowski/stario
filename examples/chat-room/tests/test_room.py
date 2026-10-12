"""Room feature — first paint and unknown-room handling."""

import pytest


@pytest.mark.asyncio
async def test_room_page(client):
    await client.post(
        "/rooms",
        json={"room_title": "General", "room_description": "Welcome chat"},
    )
    r = await client.get("/rooms/general")
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")
    assert "general" in r.text.lower()


@pytest.mark.asyncio
async def test_unknown_room_redirects(client):
    r = await client.get("/rooms/nope", follow_redirects=False)
    assert r.status_code in {301, 302, 303, 307, 308}
    assert r.headers.get("location", "/").rstrip("/") in {"", "/"}
