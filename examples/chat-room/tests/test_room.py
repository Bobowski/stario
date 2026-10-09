"""Room feature — the page loads; commands repaint the stream."""

import json


async def test_room_page_loads(client):
    await client.post("/rooms", json={"room_title": "General"})
    r = await client.get("/rooms/general")
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")
    assert "general" in r.text.lower()


async def test_unknown_room_redirects_to_lobby(client):
    r = await client.get("/rooms/nope", follow_redirects=False)
    assert r.status_code in {301, 302, 303, 307, 308}
    assert r.headers.get("location", "/").rstrip("/") in {"", "/"}


async def test_send_without_joining_is_forbidden(client):
    await client.post("/rooms", json={"room_title": "General"})
    r = await client.post(
        "/rooms/general/send",
        json={"user_id": "stranger", "username": "S", "color": "#fff", "message": "hi"},
    )
    assert r.status_code == 403


async def test_room_send_repaints_the_stream(client):
    await client.post("/rooms", json={"room_title": "General"})
    signals = json.dumps({"user_id": "u1", "username": "HappyFox", "color": "#e74c3c"})
    async with client.stream(
        "GET",
        "/rooms/general/subscribe",
        params={"datastar": signals},
        headers={"accept-encoding": "identity"},
    ) as live:
        events = live.iter_events()
        first = await anext(events)
        assert "General" in first["data"]
        await anext(events)  # repaint for this tab's own join

        r = await client.post(
            "/rooms/general/send",
            json={
                "user_id": "u1",
                "username": "HappyFox",
                "color": "#e74c3c",
                "message": "hello room",
            },
        )
        assert r.status_code == 204

        repaint = await anext(events)
        assert "hello room" in repaint["data"]
