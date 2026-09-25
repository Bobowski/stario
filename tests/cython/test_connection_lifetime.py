"""Idle and header deadlines: what may and may not keep a connection open."""

from __future__ import annotations

import asyncio

import pytest

import stario.responses as responses
from stario import App, Route
from tests.cython import h2wire as h2
from tests.cython.http import (
    RecordingTransport,
    make_protocol,
    response_statuses,
    running_server,
)

_TIMEOUT = 0.15
_TRICKLE_PAUSE = 0.05


async def _closed_within(reader: asyncio.StreamReader, seconds: float) -> bool:
    try:
        async with asyncio.timeout(seconds):
            while await reader.read(65536):
                pass
    except TimeoutError:
        return False
    except ConnectionResetError:
        pass
    return True


@pytest.mark.asyncio
async def test_h2_pings_do_not_keep_an_idle_connection_open() -> None:
    app = App()

    async def page(_c, w) -> None:
        responses.text(w, "ok")

    app.add(Route("GET /"), page)
    async with running_server(app, keep_alive_timeout=_TIMEOUT) as port:
        reader, writer, buf = await h2.h2_handshake("127.0.0.1", port)
        writer.write(
            h2.pack_frame(
                h2.TYPE_HEADERS,
                h2.FLAG_END_HEADERS | h2.FLAG_END_STREAM,
                1,
                h2.encode_request(),
            )
        )
        await h2.read_stream(reader, buf, 1)

        async def ping_forever() -> None:
            while True:
                writer.write(h2.pack_frame(h2.TYPE_PING, 0, 0, b"12345678"))
                await asyncio.sleep(_TRICKLE_PAUSE)

        pinger = asyncio.create_task(ping_forever())
        try:
            assert await _closed_within(reader, 2.5)
        finally:
            pinger.cancel()
            writer.close()


@pytest.mark.asyncio
async def test_h2_half_open_stream_is_reset_after_the_response() -> None:
    app = App()

    async def page(_c, w) -> None:
        responses.text(w, "early")

    app.add(Route("POST /"), page)
    async with running_server(app) as port:
        reader, writer, buf = await h2.h2_handshake("127.0.0.1", port)
        writer.write(
            h2.pack_frame(
                h2.TYPE_HEADERS,
                h2.FLAG_END_HEADERS,
                1,
                h2.encode_request(method="POST", path="/"),
            )
        )
        frames: list[h2.H2Frame] = []
        async with asyncio.timeout(2):
            while not h2.has_rst(frames, 1):
                buf += await reader.read(65536)
                parsed, buf = h2.parse_frames(buf)
                frames.extend(parsed)
        writer.close()
    assert h2.stream_data(frames, 1) == b"early"
    assert h2.rst_code(frames, 1) == 0


@pytest.mark.asyncio
async def test_h2_handler_reading_body_after_responding_is_not_reset() -> None:
    app = App()
    got: list[bytes] = []

    async def page(c, w) -> None:
        responses.text(w, "early")
        got.append(await c.req.body())

    app.add(Route("POST /"), page)
    async with running_server(app) as port:
        reader, writer, buf = await h2.h2_handshake("127.0.0.1", port)
        writer.write(
            h2.pack_frame(
                h2.TYPE_HEADERS,
                h2.FLAG_END_HEADERS,
                1,
                h2.encode_request(method="POST", path="/"),
            )
        )
        frames, buf = await h2.read_stream(reader, buf, 1)
        await asyncio.sleep(0.05)
        writer.write(h2.pack_frame(h2.TYPE_DATA, h2.FLAG_END_STREAM, 1, b"late"))
        async with asyncio.timeout(2):
            while not got:
                await asyncio.sleep(0.01)
        writer.close()
    assert not h2.has_rst(frames, 1)
    assert got == [b"late"]


@pytest.mark.asyncio
async def test_h1_bare_crlfs_do_not_keep_an_idle_connection_open() -> None:
    app = App()

    async def page(_c, w) -> None:
        responses.text(w, "ok")

    app.add(Route("GET /"), page)
    async with running_server(app, keep_alive_timeout=_TIMEOUT) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET / HTTP/1.1\r\nHost: t\r\n\r\n")

        async def crlf_forever() -> None:
            while True:
                await asyncio.sleep(_TRICKLE_PAUSE)
                if writer.is_closing():
                    return
                writer.write(b"\r\n")

        trickler = asyncio.create_task(crlf_forever())
        try:
            assert await _closed_within(reader, 2.5)
        finally:
            trickler.cancel()
            writer.close()


@pytest.mark.asyncio
async def test_h1_pipelined_requests_held_by_the_server_are_not_timed_out() -> None:
    loop = asyncio.get_running_loop()
    app = App()
    release = asyncio.Event()

    async def slow(_c, w) -> None:
        await release.wait()
        responses.text(w, "slow")

    async def fast(_c, w) -> None:
        responses.text(w, "fast")

    app.add(Route("GET /slow"), slow)
    app.add(Route("GET /fast"), fast)
    proto = make_protocol(loop, app, header_timeout=_TIMEOUT, max_pipelined_requests=2)
    transport = RecordingTransport(proto)
    proto.connection_made(transport)
    try:
        proto.data_received(
            b"GET /slow HTTP/1.1\r\nHost: t\r\n\r\n"
            b"GET /fast HTTP/1.1\r\nHost: t\r\n\r\n"
            b"GET /fast HTTP/1.1\r\nHost: t\r\n\r\n"
        )
        await asyncio.sleep(_TIMEOUT * 4)
        assert not transport.is_closing()
        release.set()
        await asyncio.sleep(0.05)
        await app.drain_tasks()
        assert response_statuses(transport.writes) == [200, 200, 200]
    finally:
        release.set()
        if not transport.is_closing():
            transport.close()
        await app.drain_tasks()


@pytest.mark.asyncio
async def test_h2_connection_window_caps_bodies_nobody_reads() -> None:
    app = App()
    release = asyncio.Event()

    async def upload(_c, w) -> None:
        await release.wait()
        responses.text(w, "ok")

    app.add(Route("POST /"), upload)
    streams = [1, 3, 5, 7, 9, 11, 13, 15]
    async with running_server(app) as port:
        reader, writer, buf = await h2.h2_handshake("127.0.0.1", port)
        for sid in streams:
            writer.write(
                h2.pack_frame(
                    h2.TYPE_HEADERS,
                    h2.FLAG_END_HEADERS,
                    sid,
                    h2.encode_request(method="POST", path="/"),
                )
            )
        conn_window = 65535
        stream_window = dict.fromkeys(streams, 1024 * 1024)
        sent = 0
        chunk = b"x" * 16384
        idle_rounds = 0
        while idle_rounds < 3:
            progressed = False
            for sid in streams:
                n = min(len(chunk), conn_window, stream_window[sid])
                if n > 0:
                    writer.write(h2.pack_frame(h2.TYPE_DATA, 0, sid, chunk[:n]))
                    conn_window -= n
                    stream_window[sid] -= n
                    sent += n
                    progressed = True
            await writer.drain()
            try:
                async with asyncio.timeout(0.1):
                    buf += await reader.read(65536)
            except TimeoutError:
                pass
            frames, buf = h2.parse_frames(buf)
            for frame in frames:
                if frame.type == h2.TYPE_WINDOW_UPDATE:
                    inc = int.from_bytes(frame.payload[:4], "big") & 0x7FFFFFFF
                    if frame.stream_id == 0:
                        conn_window += inc
                    elif frame.stream_id in stream_window:
                        stream_window[frame.stream_id] += inc
                    progressed = True
            idle_rounds = 0 if progressed else idle_rounds + 1
        release.set()
        writer.close()
    # 4 MiB connection window plus what streams read before they paused;
    # without the cap every stream fills its own 1 MiB window (~8.5 MiB).
    assert sent < 6 * 1024 * 1024
