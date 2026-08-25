import asyncio
import gzip
import socket
from contextlib import asynccontextmanager

import brotli
import pytest

from stario import App
from stario.exceptions import StarioError
from stario.http.compression import CompressionConfig
from stario.telemetry.noop import NoOpTracer
from stario_cython.headers import Headers
from stario_cython.protocol import HttpProtocol


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@asynccontextmanager
async def _running_server(app, *, date: bytes, compression=None):
    loop = asyncio.get_running_loop()
    connections: set[HttpProtocol] = set()
    server = await loop.create_server(
        lambda: HttpProtocol(
            loop,
            app,
            NoOpTracer(),
            [date],
            CompressionConfig() if compression is None else compression,
            connections,
        ),
        "127.0.0.1",
        _free_port(),
    )
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()
        await server.wait_closed()


async def _read_response(reader: asyncio.StreamReader) -> bytes:
    async with asyncio.timeout(2):
        header = await reader.readuntil(b"\r\n\r\n")
        for line in header.split(b"\r\n"):
            if line.lower().startswith(b"content-length:"):
                length = int(line.split(b":", 1)[1])
                return header + (await reader.readexactly(length) if length else b"")
    return header


async def _read_chunk(reader: asyncio.StreamReader) -> bytes:
    async with asyncio.timeout(2):
        size = int((await reader.readuntil(b"\r\n"))[:-2], 16)
        if size == 0:
            assert await reader.readexactly(2) == b"\r\n"
            return b""
        data = await reader.readexactly(size)
        assert await reader.readexactly(2) == b"\r\n"
        return data


def test_headers_public_api():
    headers = Headers()
    headers.set("Host", "example.com")
    headers.add("Accept", "text/html")
    headers.add("Accept", "text/plain")
    assert headers.get("host") == "example.com"
    assert headers.getlist("accept") == ["text/html", "text/plain"]
    assert "HOST" in headers
    assert len(headers) == 2
    headers.remove("accept")
    assert headers.get("accept") is None


def test_headers_unsafe_bytes():
    headers = Headers()
    headers.unsafe_add(b"user-agent", b"wrk/4")
    assert headers.unsafe_get(b"user-agent") == b"wrk/4"


@pytest.mark.parametrize(
    ("encoding", "compression"),
    [
        (
            b"br",
            CompressionConfig(
                min_size=0,
                brotli_level=4,
                brotli_window_log=18,
                zstd_level=-1,
                gzip_level=-1,
            ),
        ),
        (
            b"gzip",
            CompressionConfig(
                min_size=0,
                brotli_level=-1,
                zstd_level=-1,
                gzip_level=6,
            ),
        ),
    ],
)
@pytest.mark.asyncio
async def test_exchange_respond_native_compression_round_trip(
    encoding: bytes,
    compression: CompressionConfig,
) -> None:
    app = App()
    body = b"native compression response " * 64

    async def compressed(_c, w):
        w.respond(body, b"text/plain; charset=utf-8")

    app.get("/", compressed)
    async with _running_server(
        app,
        date=b"date: now\r\n",
        compression=compression,
    ) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(
                b"GET / HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Accept-Encoding: "
                + encoding
                + b"\r\n"
                b"Connection: close\r\n\r\n"
            )
            await writer.drain()
            payload = await _read_response(reader)
            header, compressed_body = payload.split(b"\r\n\r\n", 1)
            assert b"content-encoding: " + encoding + b"\r\n" in header + b"\r\n"
            if encoding == b"br":
                decoded = brotli.decompress(compressed_body)
            else:
                decoded = gzip.decompress(compressed_body)
            assert decoded == body
        finally:
            writer.close()
            await writer.wait_closed()


@pytest.mark.asyncio
async def test_exchange_sse_gzip_flushes_each_write() -> None:
    app = App()
    first_written = asyncio.Event()
    release_second = asyncio.Event()
    state = {}

    async def stream(_c, w):
        state["exchange"] = w
        w.headers.set("content-type", "text/event-stream")
        w.write_headers(200)
        w.write(b"data: 0\n\n")
        first_written.set()
        await release_second.wait()
        w.write(b"data: 1\n\n")
        w.end()

    app.get("/stream", stream)
    async with _running_server(
        app,
        date=b"date: now\r\n",
        compression=CompressionConfig(
            brotli_level=-1,
            zstd_level=-1,
            gzip_level=6,
        ),
    ) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(
                b"GET /stream HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Accept-Encoding: gzip\r\n"
                b"Connection: close\r\n\r\n"
            )
            await writer.drain()
            header = await reader.readuntil(b"\r\n\r\n")
            assert header.startswith(b"HTTP/1.1 200 OK\r\n")
            assert b"content-encoding: gzip\r\n" in header
            assert b"transfer-encoding: chunked\r\n" in header

            await first_written.wait()
            compressed = [_read := await _read_chunk(reader)]
            assert _read
            release_second.set()
            while chunk := await _read_chunk(reader):
                compressed.append(chunk)

            assert gzip.decompress(b"".join(compressed)) == (
                b"data: 0\n\ndata: 1\n\n"
            )
            assert state["exchange"].started
            assert state["exchange"].completed
        finally:
            writer.close()
            await writer.wait_closed()


@pytest.mark.asyncio
async def test_exchange_sse_brotli_flushes_each_write() -> None:
    app = App()
    first_written = asyncio.Event()
    release_second = asyncio.Event()

    async def stream(_c, w):
        w.headers.set("content-type", "text/event-stream")
        w.write_headers(200)
        w.write(b"data: 0\n\n")
        first_written.set()
        await release_second.wait()
        w.write(b"data: 1\n\n")
        w.end()

    app.get("/stream", stream)
    async with _running_server(
        app,
        date=b"date: now\r\n",
        compression=CompressionConfig(
            brotli_level=4,
            brotli_window_log=18,
            zstd_level=-1,
            gzip_level=-1,
        ),
    ) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(
                b"GET /stream HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Accept-Encoding: br\r\n"
                b"Connection: close\r\n\r\n"
            )
            await writer.drain()
            header = await reader.readuntil(b"\r\n\r\n")
            assert b"content-encoding: br\r\n" in header
            assert b"transfer-encoding: chunked\r\n" in header

            await first_written.wait()
            first_chunk = await _read_chunk(reader)
            assert first_chunk
            decoder = brotli.Decompressor()
            decoded = [decoder.process(first_chunk)]
            assert decoded == [b"data: 0\n\n"]

            release_second.set()
            while chunk := await _read_chunk(reader):
                decoded.append(decoder.process(chunk))
            assert b"".join(decoded) == b"data: 0\n\ndata: 1\n\n"
            assert decoder.is_finished()
        finally:
            writer.close()
            await writer.wait_closed()


@pytest.mark.asyncio
async def test_exchange_respond_plaintext_uses_date_box() -> None:
    app = App()
    state = {}

    async def plaintext(_c, w):
        w.respond(b"Hello, World!", b"text/plain; charset=utf-8", 200)
        state["exchange"] = w
        state["status"] = w.status_code
        state["completed"] = w.completed

    app.get("/", plaintext)
    async with _running_server(app, date=b"date: boxed\r\n") as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(
                b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
            payload = await _read_response(reader)
            assert payload.startswith(b"HTTP/1.1 200 OK\r\n")
            assert b"date: boxed\r\n" in payload
            assert payload.endswith(b"Hello, World!")
            assert state["status"] == 200
            assert state["completed"]
        finally:
            writer.close()
            await writer.wait_closed()


@pytest.mark.asyncio
async def test_exchange_rejects_negative_content_length() -> None:
    app = App()
    caught = asyncio.get_running_loop().create_future()

    async def invalid(_c, w):
        w.headers.unsafe_set(b"content-length", b"-1")
        try:
            w.write_headers(200)
        except StarioError as exc:
            caught.set_result(exc)
            w.abort()

    app.get("/", invalid)
    async with _running_server(app, date=b"date: now\r\n") as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
            await writer.drain()
            async with asyncio.timeout(2):
                exc = await caught
            assert "Invalid Content-Length" in str(exc)
            assert await reader.read() == b""
        finally:
            writer.close()
            await writer.wait_closed()


@pytest.mark.asyncio
async def test_respond_accepts_list_of_bytes_parts() -> None:
    app = App()

    async def multi(_c, w):
        w.respond([b"hel", b"lo", b", ", b"parts"], b"text/plain; charset=utf-8")

    app.get("/", multi)
    async with _running_server(app, date=b"date: now\r\n") as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(
                b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
            payload = await _read_response(reader)
            assert b"content-length: 12\r\n" in payload
            assert payload.endswith(b"hello, parts")
        finally:
            writer.close()
            await writer.wait_closed()


@pytest.mark.asyncio
async def test_respond_accepts_tuple_of_bytes_parts() -> None:
    app = App()

    async def multi(_c, w):
        w.respond((b"tup", b"le"), b"text/plain; charset=utf-8")

    app.get("/", multi)
    async with _running_server(app, date=b"date: now\r\n") as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(
                b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
            payload = await _read_response(reader)
            assert b"content-length: 5\r\n" in payload
            assert payload.endswith(b"tuple")
        finally:
            writer.close()
            await writer.wait_closed()


@pytest.mark.asyncio
async def test_write_known_length_accepts_list_of_bytes_parts() -> None:
    app = App()

    async def multi(_c, w):
        w.headers.unsafe_set(b"content-type", b"text/plain")
        w.headers.unsafe_set(b"content-length", b"11")
        w.write_headers(200)
        w.write([b"hello", b" ", b"world"])
        w.end()

    app.get("/", multi)
    async with _running_server(app, date=b"date: now\r\n") as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(
                b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
            payload = await _read_response(reader)
            assert payload.endswith(b"hello world")
        finally:
            writer.close()
            await writer.wait_closed()


@pytest.mark.asyncio
async def test_write_chunked_accepts_list_of_bytes_parts() -> None:
    app = App()

    async def multi(_c, w):
        w.headers.set("content-type", "text/plain")
        w.write_headers(200)
        w.write([b"aaa", b"bbb", b"ccc"])
        w.end()

    app.get("/", multi)
    async with _running_server(
        app,
        date=b"date: now\r\n",
        compression=CompressionConfig(
            brotli_level=-1, zstd_level=-1, gzip_level=-1
        ),
    ) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(
                b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
            header = await reader.readuntil(b"\r\n\r\n")
            assert b"transfer-encoding: chunked" in header.lower()
            assert await _read_chunk(reader) == b"aaabbbccc"
            assert await _read_chunk(reader) == b""
        finally:
            writer.close()
            await writer.wait_closed()


@pytest.mark.asyncio
async def test_respond_list_parts_native_compression_round_trip() -> None:
    app = App()
    parts = [b"native ", b"list ", b"compression " * 32]

    async def compressed(_c, w):
        w.respond(parts, b"text/plain; charset=utf-8")

    app.get("/", compressed)
    async with _running_server(
        app,
        date=b"date: now\r\n",
        compression=CompressionConfig(
            min_size=0,
            brotli_level=4,
            brotli_window_log=18,
            zstd_level=-1,
            gzip_level=-1,
        ),
    ) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(
                b"GET / HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Accept-Encoding: br\r\n"
                b"Connection: close\r\n\r\n"
            )
            await writer.drain()
            payload = await _read_response(reader)
            header, compressed_body = payload.split(b"\r\n\r\n", 1)
            assert b"content-encoding: br\r\n" in header + b"\r\n"
            assert brotli.decompress(compressed_body) == b"".join(parts)
        finally:
            writer.close()
            await writer.wait_closed()


@pytest.mark.asyncio
async def test_respond_rejects_non_bytes_parts() -> None:
    app = App()
    caught = asyncio.get_running_loop().create_future()

    async def bad(_c, w):
        try:
            w.respond([b"ok", "nope"], b"text/plain")  # type: ignore[list-item]
        except TypeError as exc:
            caught.set_result(exc)
            w.respond(b"err", b"text/plain", 500)

    app.get("/", bad)
    async with _running_server(app, date=b"date: now\r\n") as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
            await writer.drain()
            async with asyncio.timeout(2):
                exc = await caught
            assert "bytes-like" in str(exc)
            await _read_response(reader)
        finally:
            writer.close()
            await writer.wait_closed()
