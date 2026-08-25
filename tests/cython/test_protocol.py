import asyncio
import socket

import pytest
import uvloop

import stario.responses as responses
from stario.http.compression import CompressionConfig
from stario.telemetry.noop import NoOpTracer
from stario_cython.app import App
from stario_cython.protocol import HttpProtocol


class TrackingApp(App):
    def __init__(self) -> None:
        super().__init__()
        self.eager_starts: list[bool] = []

    def create_task(self, coro, **kwargs):
        self.eager_starts.append(kwargs.get("eager_start", False))
        return super().create_task(coro, **kwargs)


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
    app = TrackingApp()
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
        assert app.eager_starts == [True, True]
        assert writers[0] is writers[1]
        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_fragmented_headers_materialize_correctly() -> None:
    loop = asyncio.get_running_loop()
    app = App()

    async def inspect(c, w):
        assert c.req.host == "example.com"
        assert c.req.headers.getlist("x-test") == ["one", "two"]
        responses.text(w, "ok")

    app.get("/", inspect)
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
    request = (
        b"GET / HTTP/1.1\r\n"
        b"Host: Example.COM:80\r\n"
        b"X-Test: one\r\n"
        b"x-test: two\r\n"
        b"Connection: close\r\n\r\n"
    )
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        for byte in request:
            writer.write(bytes((byte,)))
            await writer.drain()
            await asyncio.sleep(0)
        assert b"ok" in await _read_response(reader)
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
async def test_abandoned_stream_discards_remainder_and_advances_pipeline() -> None:
    loop = asyncio.get_running_loop()
    app = App()

    async def partial(c, w):
        async for _ in c.req.stream():
            break
        responses.text(w, "partial")

    async def next_request(_c, w):
        responses.text(w, "next")

    app.post("/partial", partial)
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
    payload = b"x" * (2 * 1024 * 1024)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"POST /partial HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Content-Length: "
            + str(len(payload)).encode("ascii")
            + b"\r\n\r\n"
            + payload
            + b"GET /next HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Connection: close\r\n\r\n"
        )
        await writer.drain()
        assert b"partial" in await _read_response(reader)
        assert b"next" in await _read_response(reader)
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
async def test_large_single_read_pipeline_is_bounded() -> None:
    loop = asyncio.get_running_loop()
    app = App()
    release = asyncio.Event()
    handled: list[int] = []

    async def endpoint(c, w):
        index = int(c.req.query_bytes)
        handled.append(index)
        if index == 0:
            await release.wait()
        w.respond(str(index).encode("ascii"), b"text/plain")

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
    request_count = 128
    requests = []
    for index in range(request_count):
        requests.append(
            b"GET /?"
            + str(index).encode("ascii")
            + b" HTTP/1.1\r\nHost: localhost\r\n"
            + b"\r\n"
        )
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"".join(requests))
        await writer.drain()
        response = await _read_response(reader)
        assert response.startswith(b"HTTP/1.1 429")
        release.set()
        assert handled == [0]
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


@pytest.mark.asyncio
async def test_handler_starts_before_content_length_body_arrives() -> None:
    loop = asyncio.get_running_loop()
    app = App()
    started = asyncio.Event()

    async def echo(c, w):
        started.set()
        body = await c.req.body()
        w.respond(body, b"text/plain")

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
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"POST /echo HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Content-Length: 5\r\n\r\n"
        )
        await writer.drain()
        await asyncio.wait_for(started.wait(), timeout=1.0)
        writer.write(b"hello")
        await writer.drain()
        assert b"hello" in await _read_response(reader)
        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_body_wait_survives_multi_segment_upload() -> None:
    """body() must not require per-segment Event wakes — only completion."""
    loop = asyncio.get_running_loop()
    app = App()
    started = asyncio.Event()

    async def echo(c, w):
        started.set()
        body = await c.req.body()
        w.respond(body, b"text/plain")

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
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        payload = b"abcdefghij" * 1000  # 10 KiB
        writer.write(
            b"POST /echo HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Content-Length: "
            + str(len(payload)).encode("ascii")
            + b"\r\n\r\n"
        )
        await writer.drain()
        await asyncio.wait_for(started.wait(), timeout=1.0)
        # Deliver body in small segments with yields so body() is waiting.
        for i in range(0, len(payload), 512):
            writer.write(payload[i : i + 512])
            await writer.drain()
            await asyncio.sleep(0)
        assert payload in await _read_response(reader)
        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_next_request_starts_after_respond_before_handler_returns() -> None:
    """Connection advances on response send, not when the handler coroutine ends."""
    loop = asyncio.get_running_loop()
    app = App()
    first_responded = asyncio.Event()
    second_started = asyncio.Event()
    release_first = asyncio.Event()
    order: list[str] = []

    async def first(_c, w):
        order.append("first-enter")
        w.respond(b"one", b"text/plain")
        first_responded.set()
        await release_first.wait()
        order.append("first-exit")

    async def second(_c, w):
        order.append("second-enter")
        second_started.set()
        w.respond(b"two", b"text/plain")

    app.get("/first", first)
    app.get("/second", second)
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
            b"GET /first HTTP/1.1\r\nHost: localhost\r\n\r\n"
            b"GET /second HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
        )
        await writer.drain()
        assert b"one" in await _read_response(reader)
        await asyncio.wait_for(first_responded.wait(), timeout=1.0)
        await asyncio.wait_for(second_started.wait(), timeout=1.0)
        assert "second-enter" in order
        assert "first-exit" not in order
        release_first.set()
        assert b"two" in await _read_response(reader)
        await asyncio.sleep(0)
        assert order == ["first-enter", "second-enter", "first-exit"]
        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_stream_max_chunk_must_be_below_limit() -> None:
    loop = asyncio.get_running_loop()
    app = App()
    errors: list[ValueError] = []

    async def upload(c, w):
        try:
            async for _ in c.req.stream(max_chunk=256 * 1024):
                pass
        except ValueError as exc:
            errors.append(exc)
            raise
        w.respond(b"ok", b"text/plain")

    app.post("/upload", upload)
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
            b"POST /upload HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Content-Length: 1\r\n\r\n"
            b"x"
        )
        await writer.drain()
        response = await _read_response(reader)
        assert b"500" in response.split(b"\r\n", 1)[0]
        assert errors
        assert "stream chunk limit" in str(errors[0])
        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_stream_respects_max_chunk_batches() -> None:
    loop = asyncio.get_running_loop()
    app = App()
    sizes: list[int] = []

    async def upload(c, w):
        async for chunk in c.req.stream(max_chunk=8 * 1024):
            sizes.append(len(chunk))
        w.respond(str(sum(sizes)).encode("ascii"), b"text/plain")

    app.post("/upload", upload)
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
    payload = b"z" * (2 * 1024 * 1024)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"POST /upload HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Content-Length: "
            + str(len(payload)).encode("ascii")
            + b"\r\n\r\n"
            + payload
        )
        await writer.drain()
        assert b"2097152" in await _read_response(reader)
        assert sizes
        assert all(size <= 8 * 1024 for size in sizes)
        assert any(size == 8 * 1024 for size in sizes[:-1] or sizes)
        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()
