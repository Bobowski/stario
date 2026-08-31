"""Cython protocol edge cases, including header/idle timeouts and pipeline cap."""

from __future__ import annotations

import asyncio
import logging
import random

import pytest

import stario.responses as responses
from stario import App, Relay
from stario.datastar import SSE
from stario.testing.tracer import TestTracer
from tests.helpers import assert_status_span
from stario_cython.request import Request
from tests.cython.http import (
    RecordingTransport,
    make_protocol,
    response_status,
    response_statuses,
)

# Production sweeps with the Date tick (1s). Tests force 50ms via conftest.
# Timeouts here must exceed two periods; waits must exceed timeout + one period.
_TIMEOUT = 0.15
_WAIT = 0.40
_TRICKLE_PAUSE = 0.08


def _attach(app: App | None = None, **kwargs):
    loop = asyncio.get_running_loop()
    if app is None:
        app = App()
    proto = make_protocol(loop, app, **kwargs)
    transport = RecordingTransport(proto)
    proto.connection_made(transport)
    return proto, app, transport


async def _drain(app: App) -> None:
    await asyncio.sleep(0)
    await app.drain_tasks()


@pytest.mark.asyncio
async def test_garbage_bytes_yield_400() -> None:
    proto, app, transport = _attach()
    try:
        proto.data_received(b"\x00\xff\xfe not http \r\n\r\n")
        await _drain(app)
        assert response_status(transport.writes) == 400
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_upgrade_request_yields_400() -> None:
    proto, app, transport = _attach()
    try:
        proto.data_received(
            b"GET / HTTP/1.1\r\n"
            b"Host: t\r\n"
            b"Connection: Upgrade\r\n"
            b"Upgrade: websocket\r\n"
            b"\r\n"
        )
        await _drain(app)
        assert response_status(transport.writes) == 400
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_write_then_raise_logs_and_keeps_response(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """NoOp + eager complete must still log; the 200 already on the wire stays."""

    async def handler(_c, w) -> None:
        responses.text(w, "ok")
        raise RuntimeError("after write")

    app = App()
    app.get("/x", handler)
    proto, app, transport = _attach(app)
    try:
        with caplog.at_level(logging.ERROR, logger="stario.http"):
            proto.data_received(b"GET /x HTTP/1.1\r\nHost: t\r\n\r\n")
            await _drain(app)
        raw = b"".join(transport.writes)
        assert response_status(transport.writes) == 200
        assert b"ok" in raw
        assert b"500" not in raw
        assert "Handler failed" in caplog.text
        assert "after write" in caplog.text
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_invalid_incoming_header_names_return_400() -> None:
    proto, app, transport = _attach()
    try:
        proto.data_received(b"GET / HTTP/1.1\r\nBad Name: v\r\n\r\n")
        await _drain(app)
        assert response_status(transport.writes) == 400
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_invalid_incoming_header_values_return_400() -> None:
    proto, app, transport = _attach()
    try:
        proto.data_received(b"GET / HTTP/1.1\r\nX-Test: ok\x00bad\r\n\r\n")
        await _drain(app)
        assert response_status(transport.writes) == 400
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_header_total_over_limit_returns_431() -> None:
    limit = 512
    app = App()
    hits = 0

    async def handler(_c, w) -> None:
        nonlocal hits
        hits += 1
        responses.text(w, "should not run")

    app.get("/", handler)
    proto, app, transport = _attach(app=app, max_header_bytes=limit)
    try:
        pad = b"x" * (limit + 32)
        proto.data_received(b"GET / HTTP/1.1\r\nHost: t\r\nX-Pad: " + pad + b"\r\n\r\n")
        await _drain(app)
        assert response_status(transport.writes) == 431
        assert hits == 0
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_body_over_limit_returns_413() -> None:
    app = App()

    async def read_body(c, w) -> None:
        await c.req.body()
        responses.text(w, "ok")

    app.post("/", read_body)
    proto, app, transport = _attach(app=app, max_body_bytes=20)
    try:
        proto.data_received(
            b"POST / HTTP/1.1\r\nHost: t\r\nContent-Length: 100\r\n\r\n" + (b"y" * 100)
        )
        await _drain(app)
        assert response_status(transport.writes) == 413
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_declared_body_over_limit_fails_before_handler_runs() -> None:
    app = App()
    hits = 0

    async def ignore_body(_c, w) -> None:
        nonlocal hits
        hits += 1
        responses.text(w, "ok")

    app.post("/", ignore_body)
    proto, app, transport = _attach(app=app, max_body_bytes=20)
    try:
        proto.data_received(
            b"POST / HTTP/1.1\r\nHost: t\r\nContent-Length: 100\r\n\r\n"
        )
        await _drain(app)
        assert response_status(transport.writes) == 413
        assert hits == 0
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_random_inputs_do_not_raise() -> None:
    rng = random.Random(42)
    for _ in range(15):
        proto, app, transport = _attach()
        try:
            proto.data_received(rng.randbytes(rng.randint(0, 512)))
            await _drain(app)
            status = response_status(transport.writes)
            assert status in (None, 400, 404, 413, 431, 414, 505)
        finally:
            if not transport.is_closing():
                transport.close()
            await _drain(app)


@pytest.mark.asyncio
async def test_percent_encoded_path_reaches_handler() -> None:
    app = App()
    seen: list[str] = []

    async def handler(c, w) -> None:
        seen.append(c.req.path)
        responses.text(w, "ok")

    app.get("/hello world", handler)
    proto, app, transport = _attach(app=app)
    try:
        proto.data_received(b"GET /hello%20world HTTP/1.1\r\nHost: t\r\n\r\n")
        await _drain(app)
        assert response_status(transport.writes) == 200
        assert seen == ["/hello world"]
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_percent_encoded_slash_does_not_change_route_structure() -> None:
    app = App()
    seen: list[str] = []

    async def wildcard(c, w) -> None:
        seen.append(c.route.params["name"])
        responses.text(w, "wildcard")

    async def nested(_c, w) -> None:
        seen.append("nested")
        responses.text(w, "nested")

    app.get("/files/{name}", wildcard)
    app.get("/files/a/b", nested)
    proto, app, transport = _attach(app=app)
    try:
        proto.data_received(b"GET /files/a%2Fb HTTP/1.1\r\nHost: t\r\n\r\n")
        await _drain(app)
        assert response_status(transport.writes) == 200
        assert seen == ["a%2Fb"]
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        b"GET /caf% HTTP/1.1\r\nHost: t\r\n\r\n",
        b"GET /%C3 HTTP/1.1\r\nHost: t\r\n\r\n",
        b"GET /caf\xc3 HTTP/1.1\r\nHost: t\r\n\r\n",
    ],
    ids=["truncated_percent", "invalid_utf8_percent", "invalid_utf8_path"],
)
async def test_invalid_path_encoding_returns_400(payload: bytes) -> None:
    proto, app, transport = _attach()
    try:
        proto.data_received(payload)
        await _drain(app)
        assert response_status(transport.writes) == 400
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_chunked_request_body_reaches_handler() -> None:
    app = App()
    bodies: list[bytes] = []

    async def echo(c, w) -> None:
        bodies.append(await c.req.body())
        responses.text(w, "ok")

    app.post("/", echo)
    proto, app, transport = _attach(app=app)
    try:
        proto.data_received(
            b"POST / HTTP/1.1\r\n"
            b"Host: t\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"\r\n"
            b"5\r\nhello\r\n"
            b"6\r\n world\r\n"
            b"0\r\n\r\n"
        )
        await _drain(app)
        assert response_status(transport.writes) == 200
        assert bodies == [b"hello world"]
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_expect_100_continue_sends_interim_response() -> None:
    app = App()

    async def echo(c, w) -> None:
        body = await c.req.body()
        responses.text(w, body.decode())

    app.post("/", echo)
    proto, app, transport = _attach(app=app)
    try:
        proto.data_received(
            b"POST / HTTP/1.1\r\n"
            b"Host: t\r\n"
            b"Content-Length: 5\r\n"
            b"Expect: 100-continue\r\n"
            b"\r\n"
        )
        await asyncio.sleep(0)
        raw = b"".join(transport.writes)
        assert raw.startswith(b"HTTP/1.1 100 Continue\r\n\r\n")

        proto.data_received(b"hello")
        await _drain(app)
        assert response_statuses(transport.writes) == [100, 200]
        assert b"".join(transport.writes).endswith(b"hello")
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_connection_close_header_closes_socket_after_response() -> None:
    app = App()

    async def handler(_c, w) -> None:
        responses.text(w, "bye")

    app.get("/", handler)
    proto, app, transport = _attach(app=app)
    try:
        proto.data_received(b"GET / HTTP/1.1\r\nHost: t\r\nConnection: close\r\n\r\n")
        await _drain(app)
        assert response_status(transport.writes) == 200
        assert transport.is_closing()
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_keep_alive_serves_second_request_on_same_connection() -> None:
    app = App()
    hits: list[str] = []

    async def a(_c, w) -> None:
        hits.append("a")
        responses.text(w, "a")

    async def b(_c, w) -> None:
        hits.append("b")
        responses.text(w, "b")

    app.get("/a", a)
    app.get("/b", b)
    proto, app, transport = _attach(app=app)
    try:
        proto.data_received(b"GET /a HTTP/1.1\r\nHost: t\r\n\r\n")
        await _drain(app)
        assert response_status(transport.writes) == 200
        assert hits == ["a"]

        proto.data_received(b"GET /b HTTP/1.1\r\nHost: t\r\n\r\n")
        await _drain(app)
        assert response_status(transport.writes) == 200
        assert hits == ["a", "b"]
        assert not transport.is_closing()
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_pipelined_requests_are_served_in_order() -> None:
    app = App()
    order: list[str] = []

    async def slow(_c, w) -> None:
        order.append("slow.start")
        await asyncio.sleep(0.01)
        order.append("slow.end")
        responses.text(w, "slow")

    async def fast(_c, w) -> None:
        order.append("fast")
        responses.text(w, "fast")

    app.get("/slow", slow)
    app.get("/fast", fast)
    proto, app, transport = _attach(app=app)
    try:
        proto.data_received(
            b"GET /slow HTTP/1.1\r\nHost: t\r\n\r\n"
            b"GET /fast HTTP/1.1\r\nHost: t\r\n\r\n"
        )
        await _drain(app)
        await app.drain_tasks()
        await _drain(app)
        assert order == ["slow.start", "slow.end", "fast"]
        assert response_statuses(transport.writes) == [200, 200]
        raw = b"".join(transport.writes)
        assert raw.index(b"slow") < raw.index(b"fast")
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_connection_lost_signals_disconnect_without_cancelling_handler() -> None:
    app = App()
    started = asyncio.Event()
    hang = asyncio.Event()
    finished = asyncio.Event()

    async def handler(c, _w) -> None:
        started.set()
        async with c.alive():
            await hang.wait()
        finished.set()

    app.get("/", handler)
    proto, app, transport = _attach(app=app)
    try:
        proto.data_received(b"GET / HTTP/1.1\r\nHost: t\r\n\r\n")
        await started.wait()
        transport.close()
        await asyncio.sleep(0)
        await _drain(app)
        assert finished.is_set()
    finally:
        hang.set()
        await _drain(app)


@pytest.mark.asyncio
async def test_connection_lost_lets_sse_handler_run_post_alive_cleanup() -> None:
    app = App()
    relay = Relay[str]()
    cleanup_events: list[str] = []

    async def subscribe(c, w) -> None:
        async with relay.subscribe("*") as live:
            SSE(w).open()
            c.span.event("connected", {})
            async for _subject, _ in c.alive(live):
                pass
        cleanup_events.append("disconnected")
        c.span.event("disconnected", {})

    app.get("/subscribe", subscribe)
    proto, app, transport = _attach(app=app)
    try:
        proto.data_received(b"GET /subscribe HTTP/1.1\r\nHost: t\r\n\r\n")
        await asyncio.sleep(0)
        transport.close()
        await _drain(app)
        assert cleanup_events == ["disconnected"]
    finally:
        await _drain(app)


@pytest.mark.asyncio
async def test_close_if_idle_closes_keep_alive_socket() -> None:
    app = App()

    async def handler(_c, w) -> None:
        responses.text(w, "ok")

    app.get("/", handler)
    proto, app, transport = _attach(app=app)
    try:
        proto.data_received(b"GET / HTTP/1.1\r\nHost: t\r\n\r\n")
        await _drain(app)
        assert response_status(transport.writes) == 200
        assert not transport.is_closing()
        assert proto.close_if_idle() is True
        assert transport.is_closing()
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_close_if_idle_skips_in_flight_handler() -> None:
    app = App()
    started = asyncio.Event()
    hang = asyncio.Event()

    async def handler(_c, _w) -> None:
        started.set()
        await hang.wait()

    app.get("/", handler)
    proto, app, transport = _attach(app=app)
    try:
        proto.data_received(b"GET / HTTP/1.1\r\nHost: t\r\n\r\n")
        await started.wait()
        assert proto.close_if_idle() is False
        assert not transport.is_closing()
    finally:
        hang.set()
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_connection_with_no_data_times_out_headers() -> None:
    proto, app, transport = _attach(header_timeout=_TIMEOUT)
    try:
        await asyncio.sleep(_WAIT)
        assert transport.is_closing()
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_partial_headers_time_out() -> None:
    proto, app, transport = _attach(header_timeout=_TIMEOUT)
    try:
        proto.data_received(b"GET / HTTP/1.1\r\n")
        await asyncio.sleep(_WAIT)
        assert transport.is_closing()
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_trickle_headers_do_not_reset_header_timeout() -> None:
    proto, app, transport = _attach(header_timeout=_TIMEOUT)
    try:
        proto.data_received(b"GET / HTTP/1.1\r\n")
        await asyncio.sleep(_TRICKLE_PAUSE)
        proto.data_received(b"Host: t\r\n")
        await asyncio.sleep(_WAIT)
        assert transport.is_closing()
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_stalled_deferred_small_body_times_out() -> None:
    """Small Content-Length bodies dispatch at message-complete. Incomplete
    bodies must not hang: the header deadline stays armed until dispatch."""
    app = App()
    hits = 0

    async def handler(c, w) -> None:
        nonlocal hits
        hits += 1
        await c.req.body()
        responses.text(w, "ok")

    app.post("/", handler)
    proto, app, transport = _attach(app=app, header_timeout=_TIMEOUT)
    try:
        proto.data_received(
            b"POST / HTTP/1.1\r\nHost: t\r\nContent-Length: 8\r\n\r\n"
        )
        await asyncio.sleep(_WAIT)
        assert transport.is_closing()
        assert hits == 0
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_complete_request_does_not_keep_header_timer() -> None:
    app = App()

    async def handler(_c, w) -> None:
        responses.text(w, "ok")

    app.get("/", handler)
    proto, app, transport = _attach(
        app=app, header_timeout=_TIMEOUT, keep_alive_timeout=5.0
    )
    try:
        proto.data_received(b"GET / HTTP/1.1\r\nHost: t\r\n\r\n")
        await _drain(app)
        assert response_status(transport.writes) == 200
        await asyncio.sleep(_WAIT)
        assert not transport.is_closing()
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_idle_keep_alive_times_out() -> None:
    app = App()

    async def handler(_c, w) -> None:
        responses.text(w, "ok")

    app.get("/", handler)
    proto, app, transport = _attach(
        app=app, header_timeout=5.0, keep_alive_timeout=_TIMEOUT
    )
    try:
        proto.data_received(b"GET / HTTP/1.1\r\nHost: t\r\n\r\n")
        await _drain(app)
        assert response_status(transport.writes) == 200
        assert not transport.is_closing()
        await asyncio.sleep(_WAIT)
        assert transport.is_closing()
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_in_flight_handler_is_not_header_timed_out() -> None:
    app = App()
    started = asyncio.Event()
    hang = asyncio.Event()

    async def handler(_c, w) -> None:
        started.set()
        await hang.wait()
        responses.text(w, "ok")

    app.get("/", handler)
    proto, app, transport = _attach(app=app, header_timeout=_TIMEOUT)
    try:
        proto.data_received(b"GET / HTTP/1.1\r\nHost: t\r\n\r\n")
        await started.wait()
        await asyncio.sleep(_WAIT)
        assert not transport.is_closing()
    finally:
        hang.set()
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_stalled_chunked_body_aborts_without_hanging() -> None:
    """Chunked / large bodies dispatch at headers-complete. A stalled
    ``body()`` wait must abort via the shared connection sweeper, not hang.
    """
    app = App()

    async def handler(c, w) -> None:
        await c.req.body()
        responses.text(w, "ok")

    app.post("/", handler)
    proto, app, transport = _attach(
        app=app, body_timeout=_TIMEOUT, header_timeout=5.0
    )
    try:
        proto.data_received(
            b"POST / HTTP/1.1\r\nHost: t\r\nTransfer-Encoding: chunked\r\n\r\n"
            b"5\r\nhello"
        )
        await asyncio.sleep(_WAIT)
        await _drain(app)
        assert response_status(transport.writes) == 500
        assert transport.is_closing()
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_timeout_sweeper_is_one_task_per_connection_set() -> None:
    from stario_cython.timeouts import timeout_cleanup_mode

    if timeout_cleanup_mode() != "sweep":
        pytest.skip("default cleanup is the connection sweeper")
    proto, app, transport = _attach(header_timeout=5.0)
    try:
        loop = asyncio.get_running_loop()
        sweeps = getattr(loop, "_stario_timeout_sweeps", None)
        assert sweeps, "connection_made should start a sweeper"
        live = [task for task in sweeps.values() if task is not None and not task.done()]
        assert len(live) == 1
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_server_date_tick_skips_fallback_sweeper() -> None:
    from stario_cython.timeouts import DATE_TICK_SWEEP_ATTR, timeout_cleanup_mode

    if timeout_cleanup_mode() != "sweep":
        pytest.skip("default cleanup is the connection sweeper")
    loop = asyncio.get_running_loop()
    setattr(loop, DATE_TICK_SWEEP_ATTR, True)
    proto = app = transport = None
    try:
        proto, app, transport = _attach(header_timeout=5.0)
        sweeps = getattr(loop, "_stario_timeout_sweeps", None) or {}
        live = [task for task in sweeps.values() if task is not None and not task.done()]
        assert live == []
    finally:
        setattr(loop, DATE_TICK_SWEEP_ATTR, False)
        if transport is not None and not transport.is_closing():
            transport.close()
        if app is not None:
            await _drain(app)


@pytest.mark.asyncio
async def test_pipeline_cap_rejects_ninth_queued_request() -> None:
    app = App()
    release = asyncio.Event()
    handled: list[int] = []

    async def endpoint(c, w) -> None:
        index = int(c.req.query_bytes)
        handled.append(index)
        if index == 0:
            await release.wait()
        responses.text(w, str(index))

    app.get("/", endpoint)
    proto, app, transport = _attach(app=app, max_pipelined_requests=8)
    try:
        chunks = [
            b"GET /?" + str(i).encode("ascii") + b" HTTP/1.1\r\nHost: t\r\n\r\n"
            for i in range(10)
        ]
        proto.data_received(b"".join(chunks))
        await asyncio.sleep(0)
        assert response_status(transport.writes) == 429
        release.set()
        await _drain(app)
        assert handled == [0]
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


def test_request_shim_reexports_exchange_type() -> None:
    from stario_cython.exchange import Request as ExchangeRequest

    assert Request is ExchangeRequest
    req = Request(method="GET", path="/x", headers={"host": "Example.COM:80"})
    assert req.host == "example.com"


@pytest.mark.asyncio
async def test_protocol_413_finishes_span_without_fail() -> None:
    with TestTracer() as tracer:
        proto, app, transport = _attach(tracer=tracer, max_body_bytes=20)
        try:
            proto.data_received(
                b"POST /upload HTTP/1.1\r\nHost: t\r\nContent-Length: 100\r\n\r\n"
            )
            await _drain(app)
            assert response_status(transport.writes) == 413
            assert_status_span(tracer, 413, method="POST", path="/upload")
        finally:
            if not transport.is_closing():
                transport.close()
            await _drain(app)


@pytest.mark.asyncio
async def test_protocol_400_finishes_span_without_fail() -> None:
    with TestTracer() as tracer:
        proto, app, transport = _attach(tracer=tracer)
        try:
            proto.data_received(b"\x00\xff\xfe not http \r\n\r\n")
            await _drain(app)
            assert response_status(transport.writes) == 400
            assert_status_span(tracer, 400)
        finally:
            if not transport.is_closing():
                transport.close()
            await _drain(app)


@pytest.mark.asyncio
async def test_trailing_slash_308_finishes_span_without_fail() -> None:
    with TestTracer() as tracer:
        proto, app, transport = _attach(tracer=tracer)
        try:
            proto.data_received(b"GET /search/?q=cats HTTP/1.1\r\nHost: t\r\n\r\n")
            await _drain(app)
            assert response_status(transport.writes) == 308
            assert_status_span(tracer, 308, method="GET", path="/search/")
        finally:
            if not transport.is_closing():
                transport.close()
            await _drain(app)
