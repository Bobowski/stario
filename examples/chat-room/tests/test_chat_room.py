"""Smoke tests for the chat-room example."""

import pytest


@pytest.mark.asyncio
async def test_home_is_html(client):
    r = await client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")
    text = r.text.lower()
    assert "chat" in text


@pytest.mark.asyncio
async def test_about_text(client):
    r = await client.get("/about")
    assert r.status_code == 200
    assert "app.about" in r.text


@pytest.mark.asyncio
async def test_static_asset_registered(client):
    url = client.app.url_for("static:css/style.css")
    assert url.startswith("/static/")
    r = await client.get(url)
    assert r.status_code == 200
