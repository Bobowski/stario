import asyncio
import socket

import pytest
import uvloop

import stario.responses as responses
from stario import App
from stario.http.compression import CompressionConfig
from stario.telemetry.noop import NoOpTracer

from stario_cython.protocol import HttpProtocol


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _read_response(reader: asyncio.StreamReader) -> bytes:
    async with asyncio.timeout(2):
        header = await reader.readuntil(b"\r\n\r\n")
        if b"content-length:" in header.lower():
            for line in header.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    length = int(line.split(b":", 1)[1])
                    body = await reader.readexactly(length) if length else b""
                    return header + body
    return header


@pytest.mark.asyncio
async def test_plaintext_and_post_and_keepalive() -> None:
    loop = asyncio.get_running_loop()
    app = App()
    writers = []

    async def plaintext(_c, w):
        assert type(w).__name__ == "RequestExchange"
        assert _c is w
        writers.append(w)
        responses.text(w, "Hello, World!")

    async def echo(c, w):
        writers.append(w)
        body = await c.req.body()
        w.respond(body, b"text/plain; charset=utf-8", 200)

    app.get("/plaintext", plaintext)
    app.post("/echo", echo)

    connections: set[HttpProtocol] = set()
    date = b"date: Tue, 18 Aug 2026 00:00:00 GMT\r\n"

    def factory():
        return HttpProtocol(
            loop,
            app,
            NoOpTracer(),
            [date],
            CompressionConfig(),
            connections,
        )

    port = _free_port()
    server = await loop.create_server(factory, "127.0.0.1", port)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /plaintext HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        await writer.drain()
        first = await _read_response(reader)
        assert b"Hello, World!" in first
        assert all(proto.disconnect is None for proto in connections)

        writer.write(
            b"POST /echo HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Content-Length: 5\r\n"
            b"\r\n"
            b"abcde"
        )
        await writer.drain()
        second = await _read_response(reader)
        assert b"abcde" in second
        assert writers[0] is writers[1]
        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_stream_large_and_chunked_upload() -> None:
    loop = asyncio.get_running_loop()
    app = App()

    async def upload(c, w):
        total = 0
        async for chunk in c.req.stream():
            total += len(chunk)
        w.respond(str(total).encode("ascii"), b"text/plain; charset=utf-8", 200)

    app.post("/upload", upload)

    connections: set[HttpProtocol] = set()
    date = b"date: Tue, 18 Aug 2026 00:00:00 GMT\r\n"

    def factory():
        return HttpProtocol(
            loop,
            app,
            NoOpTracer(),
            [date],
            CompressionConfig(),
            connections,
        )

    port = _free_port()
    server = await loop.create_server(factory, "127.0.0.1", port)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        payload = b"x" * (70 * 1024)
        writer.write(
            b"POST /upload HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Content-Length: "
            + str(len(payload)).encode("ascii")
            + b"\r\n\r\n"
            + payload
        )
        await writer.drain()
        first = await _read_response(reader)
        assert b"71680" in first

        chunk = b"y" * 4096
        parts = []
        for _ in range(8):
            parts.append(f"{len(chunk):x}\r\n".encode("ascii") + chunk + b"\r\n")
        parts.append(b"0\r\n\r\n")
        writer.write(
            b"POST /upload HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"\r\n"
            + b"".join(parts)
        )
        await writer.drain()
        second = await _read_response(reader)
        assert b"32768" in second
        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_ignored_slow_body_stays_owned_until_message_complete() -> None:
    loop = asyncio.get_running_loop()
    app = App()
    seen = []

    async def ignore(c, w):
        seen.append(c)
        responses.text(w, "ignored")

    async def next_request(c, w):
        seen.append(c)
        responses.text(w, c.req.path)

    app.post("/ignore", ignore)
    app.get("/next", next_request)
    connections: set[HttpProtocol] = set()
    server = await loop.create_server(
        lambda: HttpProtocol(
            loop,
            app,
            NoOpTracer(),
            [b"date: Tue, 18 Aug 2026 00:00:00 GMT\r\n"],
            CompressionConfig(),
            connections,
        ),
        "127.0.0.1",
        _free_port(),
    )
    port = server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"POST /ignore HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n"
            b"4\r\nslow\r\n"
        )
        await writer.drain()
        assert b"ignored" in await _read_response(reader)

        writer.write(
            b"0\r\n\r\n"
            b"GET /next HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
        )
        await writer.drain()
        assert b"/next" in await _read_response(reader)
        assert len(seen) == 2
        assert seen[0] is seen[1]
        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_small_expect_continue_is_sent_before_body() -> None:
    loop = asyncio.get_running_loop()
    app = App()
    started = asyncio.Event()

    async def echo(c, w):
        started.set()
        w.respond(await c.req.body(), b"text/plain")

    app.post("/echo", echo)
    connections: set[HttpProtocol] = set()
    server = await loop.create_server(
        lambda: HttpProtocol(
            loop,
            app,
            NoOpTracer(),
            [b"date: Tue, 18 Aug 2026 00:00:00 GMT\r\n"],
            CompressionConfig(),
            connections,
        ),
        "127.0.0.1",
        _free_port(),
    )
    port = server.sockets[0].getsockname()[1]
    writer = None
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"POST /echo HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Content-Length: 5\r\n"
            b"Expect: 100-continue\r\n\r\n"
        )
        await writer.drain()
        async with asyncio.timeout(2):
            await started.wait()
            assert await reader.readuntil(b"\r\n\r\n") == (
                b"HTTP/1.1 100 Continue\r\n\r\n"
            )

        writer.write(b"hello")
        await writer.drain()
        assert b"hello" in await _read_response(reader)
    finally:
        if writer is not None:
            writer.close()
            await writer.wait_closed()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_disconnect_future_is_lazy() -> None:
    loop = asyncio.get_running_loop()
    app = App()

    async def watch(c, w):
        assert c.disconnected is False
        fut = c.disconnect
        assert not fut.done()
        responses.text(w, "ok")

    app.get("/watch", watch)
    connections: set[HttpProtocol] = set()
    date = b"date: Tue, 18 Aug 2026 00:00:00 GMT\r\n"

    def factory():
        return HttpProtocol(
            loop,
            app,
            NoOpTracer(),
            [date],
            CompressionConfig(),
            connections,
        )

    port = _free_port()
    server = await loop.create_server(factory, "127.0.0.1", port)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /watch HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        await writer.drain()
        payload = await _read_response(reader)
        assert b"ok" in payload
        assert all(proto.disconnect is not None for proto in connections)
        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_pipeline_waits_for_handler_and_uses_each_request_keepalive() -> None:
    loop = asyncio.get_running_loop()
    app = App()
    events = []
    exchanges = {}

    async def first(c, w):
        exchanges["first"] = c
        assert c is w
        responses.text(w, "first")
        await asyncio.sleep(0)
        events.append(("first-done", c.req.path))

    async def second(c, w):
        exchanges["second"] = c
        assert c is w
        assert c is not exchanges["first"]
        events.append(("second-start", c.req.path))
        responses.text(w, "second")

    app.get("/first", first)
    app.get("/second", second)
    connections: set[HttpProtocol] = set()
    date_box = [b"date: Tue, 18 Aug 2026 00:00:00 GMT\r\n"]

    server = await loop.create_server(
        lambda: HttpProtocol(
            loop,
            app,
            NoOpTracer(),
            date_box,
            CompressionConfig(),
            connections,
        ),
        "127.0.0.1",
        _free_port(),
    )
    port = server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"GET /first HTTP/1.1\r\nHost: localhost\r\n\r\n"
            b"GET /second HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
        )
        await writer.drain()
        assert b"first" in await _read_response(reader)
        assert b"second" in await _read_response(reader)
        assert events == [("second-start", "/second"), ("first-done", "/first")]
        assert exchanges["first"].completed
        assert exchanges["second"].completed
        assert await reader.read() == b""
        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_pipelined_streaming_bodies_are_request_owned() -> None:
    loop = asyncio.get_running_loop()
    app = App()

    async def upload(c, w):
        total = 0
        async for chunk in c.req.stream():
            total += len(chunk)
        w.respond(str(total).encode("ascii"), b"text/plain; charset=utf-8")

    app.post("/upload", upload)
    connections: set[HttpProtocol] = set()
    date_box = [b"date: Tue, 18 Aug 2026 00:00:00 GMT\r\n"]
    server = await loop.create_server(
        lambda: HttpProtocol(
            loop,
            app,
            NoOpTracer(),
            date_box,
            CompressionConfig(),
            connections,
        ),
        "127.0.0.1",
        _free_port(),
    )
    port = server.sockets[0].getsockname()[1]
    first = b"a" * (66 * 1024)
    second = b"b" * (67 * 1024)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"POST /upload HTTP/1.1\r\nHost: localhost\r\nContent-Length: "
            + str(len(first)).encode()
            + b"\r\n\r\n"
            + first
            + b"POST /upload HTTP/1.1\r\nHost: localhost\r\nContent-Length: "
            + str(len(second)).encode()
            + b"\r\n\r\n"
            + second
        )
        await writer.drain()
        assert str(len(first)).encode() in await _read_response(reader)
        assert str(len(second)).encode() in await _read_response(reader)
        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_exchange_pool_reuses_across_connections() -> None:
    loop = asyncio.get_running_loop()
    app = App()
    exchanges = []

    async def endpoint(c, w):
        assert c is w
        exchanges.append(c)
        responses.text(w, "ok")

    app.get("/", endpoint)
    connections: set[HttpProtocol] = set()
    server = await loop.create_server(
        lambda: HttpProtocol(
            loop,
            app,
            NoOpTracer(),
            [b"date: Tue, 18 Aug 2026 00:00:00 GMT\r\n"],
            CompressionConfig(),
            connections,
        ),
        "127.0.0.1",
        _free_port(),
    )
    port = server.sockets[0].getsockname()[1]
    try:
        for _ in range(2):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(
                b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
            assert b"ok" in await _read_response(reader)
            assert await reader.read() == b""
            writer.close()
            await writer.wait_closed()
            await asyncio.sleep(0)
        assert exchanges[0] is exchanges[1]
    finally:
        server.close()
        await server.wait_closed()
