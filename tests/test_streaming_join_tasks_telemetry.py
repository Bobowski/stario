"""Streaming, client teardown, and telemetry (``TestClient`` drains work on exit)."""

import asyncio

import pytest

import stario.responses as responses
from stario import App
from stario.testing import TestClient


@pytest.mark.asyncio
async def test_empty_response_then_background_child_span_finished_on_client_exit() -> None:
    """Handler returns **204** immediately; background work uses a **child** span.

    The request span is snapshotted when the handler returns. Later attribute updates on the root
    span are ignored by ``TestTracer``—record post-response work on a child span. Leaving
    ``async with TestClient(...)`` awaits ``app.create_task`` and open spans (same as app shutdown).
    """

    app = App()

    async def handler(c, w) -> None:
        followup = c.span.step("work.after_response")
        followup.start()

        async def bg() -> None:
            try:
                await asyncio.sleep(0.02)
                followup.attr("work.result", "persisted")
                followup.attr("work.rows", 2)
                followup.event("persisted", {"phase": "done"})
            finally:
                followup.end()

        c.app.create_task(bg())
        responses.empty(w, 204)

    app.post("/action", handler)

    async with TestClient(app) as client:
        r = await client.post("/action")
        assert r.status_code == 204
        root = client.tracer.get_span(r.span_id)
        assert root is not None
        assert not client.tracer.has_attribute(root.id, "work.result")

    child = client.tracer.find_span("work.after_response", root_id=r.span_id)
    assert child is not None
    assert child.attributes.get("work.result") == "persisted"
    assert child.attributes.get("work.rows") == 2
    ev = client.tracer.get_event(child.id, "persisted")
    assert ev is not None
    assert ev.attributes.get("phase") == "done"


@pytest.mark.asyncio
async def test_stream_exit_signals_disconnect_and_finishes_handler() -> None:
    """Leaving ``async with client.stream`` disconnects and waits for the handler (no manual API)."""

    app = App()

    async def handler(c, w) -> None:
        w.headers.set("content-type", "text/event-stream")
        w.write_headers(200)
        sent = 0
        while sent < 50:
            if w.disconnected:
                c.span.attr("stream.events_sent", sent)
                c.span.event("stream.client_disconnected", {"at_least_one": True})
                return
            w.write(f"event: tick\ndata: {sent}\n\n".encode())
            sent += 1
            await asyncio.sleep(0)

    app.get("/live", handler)

    async with TestClient(app) as client:
        async with client.stream(
            "GET",
            "/live",
            headers={"Accept-Encoding": "identity"},
        ) as r:
            rid = r.span_id
            seen = 0
            async for _ev in r.iter_events():
                seen += 1
                if seen >= 3:
                    break

        root = client.tracer.get_span(rid)
        assert root is not None
        assert root.attributes.get("stream.events_sent", 0) >= 3
        assert client.tracer.has_event(rid, "stream.client_disconnected")
