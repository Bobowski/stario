"""Static assets — fingerprinted URLs resolve through the app."""

import pytest

from app.assets import ASSETS


@pytest.mark.asyncio
async def test_static_asset_registered(client):
    url = ASSETS.href("css/style.css")
    assert url.startswith("/static/")
    r = await client.get(url)
    assert r.status_code == 200
