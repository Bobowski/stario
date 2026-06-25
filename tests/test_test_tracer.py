"""`TestTracer` query helpers on `TestClient`."""

import asyncio

import pytest

import stario.responses as responses
from stario import App
from stario.testing import TestClient
from stario.testing import TestTracer as StarioTestTracer


def test_test_tracer_requires_entry_before_create() -> None:
    tracer = StarioTestTracer()

    with pytest.raises(RuntimeError, match="must be entered"):
        tracer.create("request")

    with tracer:
        span = tracer.create("request")
        span.start()
        span.end()


async def _telemetry_bootstrap(app: App, span):
    async def handler(c, w):
        c.span.attr("fixture.user", "ada")
        c.span.event("milestones.started", {"phase": "a"})
        with c.span.step("db.query", {"table": "items"}) as s:
            s.attr("rows", 3)
            s.event("slow", {"ms": 12})
        responses.text(w, "ok")

    app.get("/t", handler)
    yield


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
async def test_find_span_root_id_scopes_to_request_subtree() -> None:
    app = App()

    async def handler(c, w):
        with c.span.step("work") as s:
            s.attr("route", c.req.path)
        responses.text(w, "ok")

    app.get("/a", handler)
    app.get("/b", handler)

    async with TestClient(app) as client:
        r1 = await client.get("/a")
        r2 = await client.get("/b")
        t = client.tracer

        child1 = t.find_span("work", root_id=r1.span_id)
        child2 = t.find_span("work", root_id=r2.span_id)
        assert child1 is not None
        assert child2 is not None
        assert child1.id != child2.id
        assert child1.attributes.get("route") == "/a"
        assert child2.attributes.get("route") == "/b"


@pytest.mark.asyncio
async def test_get_span_returns_none_while_span_open() -> None:
    gate = asyncio.Event()
    app = App()

    async def handler(c, w):
        with c.span.step("slow") as s:
            await gate.wait()
            s.attr("done", True)
        responses.text(w, "ok")

    app.get("/slow", handler)

    async with TestClient(app) as client:
        task = asyncio.create_task(client.get("/slow"))
        await asyncio.sleep(0)
        assert client.tracer.has_open_spans()
        gate.set()
        r = await task
        assert r.status_code == 200
        assert client.tracer.get_span(r.span_id) is not None
