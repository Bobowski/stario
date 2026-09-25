"""Response framing: HEAD, Content-Length, trailers, and caller-owned buffers."""

from __future__ import annotations

import array
import asyncio

import pytest

import stario.responses as responses
from stario import App, Route
from stario.http.compression import CompressionConfig
from tests.cython import h2wire as h2
from tests.cython.http import read_response, running_server


def _h2_get(stream_id: int, path: str, *, method: str = "GET", extra=None) -> bytes:
    return h2.pack_frame(
        h2.TYPE_HEADERS,
        h2.FLAG_END_HEADERS | h2.FLAG_END_STREAM,
        stream_id,
        h2.encode_request(method=method, path=path, extra=extra),
    )


@pytest.mark.asyncio
async def test_head_with_accept_encoding_sends_no_body() -> None:
    app = App()

    async def page(_c, w) -> None:
        responses.text(w, "x" * 4096)

    app.add(Route("GET /"), page)
    app.add(Route("HEAD /"), page)
    async with running_server(app, compression=CompressionConfig(min_size=1)) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"HEAD / HTTP/1.1\r\nHost: t\r\nAccept-Encoding: gzip\r\n\r\n"
            b"GET / HTTP/1.1\r\nHost: t\r\n\r\n"
        )
        async with asyncio.timeout(2):
            head = await reader.readuntil(b"\r\n\r\n")
        second = await read_response(reader)
        writer.close()
    assert head.startswith(b"HTTP/1.1 200")
    assert b"content-encoding" not in head.lower()
    assert second.startswith(b"HTTP/1.1 200")
    assert second.endswith(b"x" * 4096)


@pytest.mark.asyncio
async def test_h2_head_with_accept_encoding_sends_no_data() -> None:
    app = App()

    async def page(_c, w) -> None:
        responses.text(w, "x" * 4096)

    app.add(Route("GET /"), page)
    app.add(Route("HEAD /"), page)
    async with running_server(app, compression=CompressionConfig(min_size=1)) as port:
        reader, writer, buf = await h2.h2_handshake("127.0.0.1", port)
        writer.write(
            _h2_get(1, "/", method="HEAD", extra=[(b"accept-encoding", b"gzip")])
        )
        frames, _buf = await h2.read_stream(reader, buf, 1)
        writer.close()
    assert b"\x88" in h2.stream_headers_blob(frames, 1)
    assert h2.stream_data(frames, 1) == b""
    assert not h2.has_rst(frames, 1)


@pytest.mark.asyncio
async def test_h2_handler_keeps_running_after_end() -> None:
    app = App()
    after_end = asyncio.Event()

    async def page(_c, w) -> None:
        w.headers.set("content-type", "text/plain")
        w.write(b"done")
        w.end()
        await asyncio.sleep(0.01)
        after_end.set()

    app.add(Route("GET /"), page)
    async with running_server(app) as port:
        reader, writer, buf = await h2.h2_handshake("127.0.0.1", port)
        writer.write(_h2_get(1, "/"))
        frames, _buf = await h2.read_stream(reader, buf, 1)
        async with asyncio.timeout(2):
            await after_end.wait()
        writer.close()
    assert h2.stream_data(frames, 1) == b"done"


@pytest.mark.asyncio
async def test_chunked_trailers_are_not_merged_into_headers() -> None:
    app = App()
    seen: list[object] = []

    async def upload(c, w) -> None:
        body = await c.req.body()
        seen.append(c.req.headers.get("x-trailer"))
        responses.text(w, body.decode())

    app.add(Route("POST /"), upload)
    async with running_server(app) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"POST / HTTP/1.1\r\nHost: t\r\nTransfer-Encoding: chunked\r\n\r\n"
            b"3\r\nabc\r\n0\r\nX-Trailer: injected\r\n\r\n"
        )
        response = await read_response(reader)
        writer.close()
    assert response.startswith(b"HTTP/1.1 200")
    assert response.endswith(b"abc")
    assert seen == [None]


@pytest.mark.asyncio
async def test_oversized_trailers_do_not_break_the_request() -> None:
    app = App()

    async def upload(c, w) -> None:
        responses.text(w, (await c.req.body()).decode())

    app.add(Route("POST /"), upload)
    async with running_server(app, max_header_bytes=1024) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"POST / HTTP/1.1\r\nHost: t\r\nTransfer-Encoding: chunked\r\n\r\n"
            b"3\r\nabc\r\n0\r\nX-Big: " + b"a" * 2048 + b"\r\n\r\n"
            b"GET /missing HTTP/1.1\r\nHost: t\r\n\r\n"
        )
        first = await read_response(reader)
        second = await read_response(reader)
        writer.close()
    assert first.startswith(b"HTTP/1.1 200")
    assert first.endswith(b"abc")
    assert second.startswith(b"HTTP/1.1 404")


@pytest.mark.asyncio
@pytest.mark.parametrize("http2", [False, True])
async def test_write_past_content_length_raises(http2: bool) -> None:
    app = App()
    errors: list[str] = []

    async def page(_c, w) -> None:
        w.headers.set("content-type", "text/plain")
        w.headers.set("content-length", "3")
        try:
            w.write(b"abcd")
        except Exception as exc:
            errors.append(type(exc).__name__)
        w.write(b"abc")
        w.end()

    app.add(Route("GET /"), page)
    async with running_server(app) as port:
        if http2:
            reader, writer, buf = await h2.h2_handshake("127.0.0.1", port)
            writer.write(_h2_get(1, "/"))
            frames, _buf = await h2.read_stream(reader, buf, 1)
            body = h2.stream_data(frames, 1)
        else:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"GET / HTTP/1.1\r\nHost: t\r\n\r\n")
            body = (await read_response(reader)).split(b"\r\n\r\n", 1)[1]
        writer.close()
    assert errors == ["StarioRuntime"]
    assert body == b"abc"


@pytest.mark.asyncio
async def test_h2_write_copies_a_callers_bytearray() -> None:
    app = App()

    async def page(_c, w) -> None:
        w.headers.set("content-type", "text/plain")
        chunk = bytearray(b"first")
        w.write(chunk)
        chunk[:] = b"XXXXX"
        w.write(bytearray(b"-second"))
        w.end()

    app.add(Route("GET /"), page)
    async with running_server(app) as port:
        reader, writer, buf = await h2.h2_handshake("127.0.0.1", port)
        writer.write(_h2_get(1, "/"))
        frames, _buf = await h2.read_stream(reader, buf, 1)
        writer.close()
    assert h2.stream_data(frames, 1) == b"first-second"


@pytest.mark.asyncio
async def test_wide_memoryview_body_is_rejected() -> None:
    app = App()
    errors: list[str] = []

    async def page(_c, w) -> None:
        wide = memoryview(array.array("i", [1, 2]))
        try:
            w.write(wide)
        except TypeError as exc:
            errors.append(str(exc))
        responses.text(w, "ok")

    app.add(Route("GET /"), page)
    async with running_server(app) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET / HTTP/1.1\r\nHost: t\r\n\r\n")
        response = await read_response(reader)
        writer.close()
    assert response.endswith(b"ok")
    assert len(errors) == 1
    assert "itemsize" in errors[0]
