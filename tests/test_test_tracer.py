"""``TestTracer`` query helpers on ``TestClient``."""

import asyncio

import pytest

import stario.responses as responses
from stario import App
from stario.testing import TestClient


async def _telemetry_bootstrap(app: App, span) -> None:
    async def handler(c, w):
        c.span.attr("fixture.user", "ada")
        c.span.event("milestones.started", {"phase": "a"})
        with c.span.step("db.query", {"table": "items"}) as s:
            s.attr("rows", 3)
            s.event("slow", {"ms": 12})
        responses.text(w, "ok")

    app.get("/t", handler)


@pytest.mark.asyncio
async def test_test_tracer_queries_via_response_span_id() -> None:
    async with TestClient(_telemetry_bootstrap) as client:
        r = await client.get("/t")
        assert r.status_code == 200
        rid = r.span_id
        t = client.tracer
        assert t.has_attribute(rid, "fixture.user", "ada")
        assert t.has_event(rid, "milestones.started")
        ev = t.get_event(rid, "milestones.started")
        assert ev is not None
        assert ev.attributes.get("phase") == "a"

        child = t.find_span("db.query", root_id=rid)
        assert child is not None
        assert child.attributes.get("table") == "items"
        assert child.attributes.get("rows") == 3
        assert t.get_event(child.id, "slow") is not None

        assert t.get_span(rid) is not None
        assert t.get_span(child.id) is not None


@pytest.mark.asyncio
async def test_async_test_client_tracer() -> None:
    async with TestClient(_telemetry_bootstrap) as client:
        r = await client.get("/t")
        rid = r.span_id
        assert client.tracer.find_span("db.query", root_id=rid) is not None


@pytest.mark.asyncio
async def test_async_disconnect_signals_writer() -> None:
    app = App()
    done = asyncio.Event()

    async def handler(c, w):
        async def watch():
            while not w.disconnected:
                await asyncio.sleep(0)
            done.set()

        c.app.create_task(watch())
        responses.text(w, "ok")

    app.get("/", handler)
    async with TestClient(app) as client:
        r = await client.get("/")
        assert r.status_code == 200
        assert not done.is_set()
    assert done.is_set()
