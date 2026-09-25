"""Pipelined error order, upload backpressure, HTTP/2 drain, and small limits."""

from __future__ import annotations

import asyncio

import pytest

import stario.responses as responses
from stario import App, Route
from tests.cython import h2wire as h2
from tests.cython.http import (
    RecordingTransport,
    make_protocol,
    read_response,
    response_statuses,
    running_server,
)


def _status(response: bytes) -> int:
    return int(response.split(b" ", 2)[1])


def _h2_request(
    stream_id: int, path: str, *, end_stream: bool = True, extra=None
) -> bytes:
    flags = h2.FLAG_END_HEADERS | (h2.FLAG_END_STREAM if end_stream else 0)
    method = "GET" if end_stream else "POST"
    return h2.pack_frame(
        h2.TYPE_HEADERS,
        flags,
        stream_id,
        h2.encode_request(method=method, path=path, extra=extra),
    )


@pytest.mark.asyncio
async def test_closing_error_waits_behind_in_flight_pipelined_response() -> None:
    app = App()
    ran: list[str] = []

    async def post(c, w) -> None:
        await asyncio.sleep(0.05)
        ran.append(c.req.path)
        responses.text(w, "posted")

    app.add(Route("POST /a"), post)
    async with running_server(app) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"POST /a HTTP/1.1\r\nHost: t\r\nContent-Length: 0\r\n\r\nGARBAGE\r\n\r\n"
        )
        first = await read_response(reader)
        second = await read_response(reader)
        assert await reader.read() == b""
        writer.close()
    assert _status(first) == 200
    assert first.endswith(b"posted")
    assert _status(second) == 400
    assert ran == ["/a"]


@pytest.mark.asyncio
async def test_keep_alive_431_is_answered_in_pipeline_order() -> None:
    app = App()

    async def slow(_c, w) -> None:
        await asyncio.sleep(0.05)
        responses.text(w, "slow")

    async def fast(_c, w) -> None:
        responses.text(w, "fast")

    app.add(Route("GET /slow"), slow)
    app.add(Route("GET /fast"), fast)
    async with running_server(app, max_header_bytes=1024) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"GET /slow HTTP/1.1\r\nHost: t\r\n\r\n"
            b"GET /fast HTTP/1.1\r\nHost: t\r\nX-Big: " + b"b" * 2000 + b"\r\n\r\n"
            b"GET /fast HTTP/1.1\r\nHost: t\r\n\r\n"
        )
        statuses = [_status(await read_response(reader)) for _ in range(3)]
        writer.close()
    assert statuses == [200, 431, 200]


@pytest.mark.asyncio
async def test_many_small_headers_hit_the_header_budget() -> None:
    app = App()

    async def ok(_c, w) -> None:
        responses.text(w, "ok")

    app.add(Route("GET /"), ok)
    async with running_server(app) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        many = b"".join(b"%x:\r\n" % (i % 16) for i in range(4000))
        writer.write(b"GET / HTTP/1.1\r\nHost: t\r\n" + many + b"\r\n")
        response = await read_response(reader)
        writer.close()
    assert _status(response) == 431


@pytest.mark.asyncio
async def test_unread_upload_pauses_reading_until_the_handler_asks() -> None:
    loop = asyncio.get_running_loop()
    app = App()
    release = asyncio.Event()
    sizes: list[int] = []

    async def upload(c, w) -> None:
        await release.wait()
        sizes.append(len(await c.req.body()))
        responses.text(w, "ok")

    app.add(Route("POST /up"), upload)
    proto = make_protocol(loop, app)
    transport = RecordingTransport(proto)
    proto.connection_made(transport)
    total = 1024 * 1024
    try:
        proto.data_received(
            b"POST /up HTTP/1.1\r\nHost: t\r\nContent-Length: %d\r\n\r\n" % total
        )
        proto.data_received(b"x" * (128 * 1024))
        await asyncio.sleep(0)
        assert transport.reading_calls == ["pause"]
        release.set()
        await asyncio.sleep(0)
        assert transport.reading_calls == ["pause", "resume"]
        sent = 128 * 1024
        while sent < total:
            proto.data_received(b"x" * (64 * 1024))
            sent += 64 * 1024
            await asyncio.sleep(0)
        await app.drain_tasks()
        assert sizes == [total]
        assert response_statuses(transport.writes) == [200]
    finally:
        if not transport.is_closing():
            transport.close()
        await app.drain_tasks()


@pytest.mark.asyncio
async def test_h2_slow_body_consumer_does_not_stall_other_streams() -> None:
    app = App()
    release = asyncio.Event()

    async def upload(c, w) -> None:
        await release.wait()
        responses.text(w, str(len(await c.req.body())))

    async def fast(_c, w) -> None:
        responses.text(w, "fast")

    app.add(Route("POST /up"), upload)
    app.add(Route("GET /fast"), fast)
    body = b"x" * (600 * 1024)
    async with running_server(app) as port:
        reader, writer, buf = await h2.h2_handshake("127.0.0.1", port)
        writer.write(
            _h2_request(
                1,
                "/up",
                end_stream=False,
                extra=[(b"content-length", str(len(body)).encode())],
            )
        )
        # Initial client-side windows are 64 KiB; send what fits, then keep
        # sending as the server grants credit.
        writer.write(h2.pack_frame(h2.TYPE_DATA, 0, 1, body[:16384]))
        await asyncio.sleep(0.05)
        writer.write(_h2_request(3, "/fast"))
        frames, buf = await h2.read_stream(reader, buf, 3)
        assert h2.stream_data(frames, 3) == b"fast"
        assert not h2.stream_ended(frames, 1)
        release.set()
        offset = 16384
        pending = frames
        while offset < len(body):
            chunk = body[offset : offset + 16384]
            offset += len(chunk)
            flags = h2.FLAG_END_STREAM if offset >= len(body) else 0
            writer.write(h2.pack_frame(h2.TYPE_DATA, flags, 1, chunk))
        more, buf = await h2.read_stream(reader, buf, 1, timeout=5)
        writer.close()
    assert h2.stream_data(pending + more, 1) == str(len(body)).encode()


@pytest.mark.asyncio
async def test_h2_drain_sends_goaway_and_finishes_in_flight_streams() -> None:
    loop = asyncio.get_running_loop()
    app = App()
    release = asyncio.Event()
    connections: set = set()

    async def slow(_c, w) -> None:
        await release.wait()
        responses.text(w, "done")

    app.add(Route("GET /slow"), slow)
    server = await loop.create_server(
        lambda: make_protocol(loop, app, connections=connections), "127.0.0.1", 0
    )
    port = server.sockets[0].getsockname()[1]
    try:
        reader, writer, buf = await h2.h2_handshake("127.0.0.1", port)
        writer.write(_h2_request(1, "/slow"))
        await asyncio.sleep(0.05)
        (proto,) = list(connections)
        assert proto.close_if_idle() is False
        await asyncio.sleep(0.02)
        release.set()
        data = buf
        async with asyncio.timeout(2):
            while chunk := await reader.read(65536):
                data += chunk
        writer.close()
        frames, _ = h2.parse_frames(data)
        goaway = [f for f in frames if f.type == h2.TYPE_GOAWAY]
        assert goaway
        # last-stream-id 1: the in-flight stream is still served.
        assert int.from_bytes(goaway[0].payload[:4], "big") == 1
        assert h2.stream_data(frames, 1) == b"done"
    finally:
        server.close()
        await server.wait_closed()
        await app.drain_tasks()


@pytest.mark.asyncio
async def test_h2_idle_connection_closes_with_goaway_on_drain() -> None:
    loop = asyncio.get_running_loop()
    app = App()
    connections: set = set()
    server = await loop.create_server(
        lambda: make_protocol(loop, app, connections=connections), "127.0.0.1", 0
    )
    port = server.sockets[0].getsockname()[1]
    try:
        reader, writer, buf = await h2.h2_handshake("127.0.0.1", port)
        await asyncio.sleep(0.02)
        (proto,) = list(connections)
        assert proto.close_if_idle() is True
        data = buf
        async with asyncio.timeout(2):
            while chunk := await reader.read(65536):
                data += chunk
        frames, _ = h2.parse_frames(data)
        assert h2.has_goaway(frames)
        writer.close()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_fully_qualified_host_matches_host_routes() -> None:
    app = App()
    seen: list[str] = []

    async def hosted(c, w) -> None:
        seen.append(c.req.host)
        responses.text(w, "hosted")

    app.add(Route("GET //api.example.com/x"), hosted)
    async with running_server(app) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /x HTTP/1.1\r\nHost: API.example.com.:443\r\n\r\n")
        response = await read_response(reader)
        writer.close()
    assert _status(response) == 200
    assert seen == ["api.example.com"]


@pytest.mark.asyncio
async def test_handler_start_failure_answers_500_and_keeps_serving() -> None:
    class BoomSpan:
        def start(self) -> None:
            raise RuntimeError("exporter down")

        def __getattr__(self, _name):
            return lambda *_a, **_k: None

    class BoomTracer:
        def create(self, _name):
            return BoomSpan()

    loop = asyncio.get_running_loop()
    app = App()

    async def ok(_c, w) -> None:
        responses.text(w, "ok")

    app.add(Route("GET /"), ok)
    if True:
        proto = make_protocol(loop, app, tracer=BoomTracer())
        transport = RecordingTransport(proto)
        proto.connection_made(transport)
        try:
            proto.data_received(
                b"GET / HTTP/1.1\r\nHost: t\r\n\r\nGET / HTTP/1.1\r\nHost: t\r\n\r\n"
            )
            await app.drain_tasks()
            assert response_statuses(transport.writes) == [500, 500]
            assert not transport.is_closing()
        finally:
            if not transport.is_closing():
                transport.close()
            await app.drain_tasks()
