import asyncio

import pytest
from jinja2 import UndefinedError
from main import bootstrap, templates

from stario.testing import TestClient


async def test_shared_counter_updates_and_reconnect():
    async with TestClient(bootstrap) as client:
        page = await client.get("/")
        assert page.status_code == 200
        assert "text/html" in page.headers.get("content-type", "")
        assert "<strong>0</strong>" in page.text

        async with (
            client.stream("GET", "/subscribe") as first,
            client.stream("GET", "/subscribe") as second,
            asyncio.timeout(5),
        ):
            streams = [first.iter_events(), second.iter_events()]

            async def expect_count(count):
                for events in streams:
                    event = await anext(events)
                    assert event["event"] == "datastar-patch-elements"
                    assert 'id="counter"' in event["data"]
                    assert f"<strong>{count}</strong>" in event["data"]
                    assert "<main" not in event["data"]

            await expect_count(0)
            for count in (1, 2):
                assert (await client.post("/increment")).status_code == 204
                await expect_count(count)
            assert (await client.post("/reset")).status_code == 204
            await expect_count(0)

        # Changes while disconnected must appear on the next subscription.
        await client.post("/increment")
        async with client.stream("GET", "/subscribe") as reconnected:
            async with asyncio.timeout(5):
                event = await anext(reconnected.iter_events())
                assert "<strong>1</strong>" in event["data"]
        assert "<strong>1</strong>" in (await client.get("/")).text

    # Each app bootstrap owns its counter rather than sharing module state.
    async with TestClient(bootstrap) as fresh:
        assert "<strong>0</strong>" in (await fresh.get("/")).text


def test_jinja_fragment_escapes_values_and_requires_context():
    fragment = templates.get_template("counter.html.jinja")
    assert "&lt;script&gt;&amp;" in fragment.render(count="<script>&")
    with pytest.raises(UndefinedError):
        fragment.render()
