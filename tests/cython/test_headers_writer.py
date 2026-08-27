import asyncio
import gzip

import brotli
import pytest

from stario import App
from stario.exceptions import StarioError
from stario.http.compression import CompressionConfig
from stario_cython.headers import Headers
from tests.cython.http import read_chunk, read_response, running_server


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
    async with running_server(
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
            payload = await read_response(reader)
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
async def test_one_shot_compression_writes_generated_headers_without_dict_roundtrip() -> None:
    app = App()
    state = {}
    body = b"direct generated response headers " * 32

    async def compressed(_c, w):
        w.headers.set("vary", "origin")
        w.headers.set("x-custom", "present")
        w.respond(body, b"text/plain; charset=utf-8")
        state["content-encoding"] = w.headers.get("content-encoding")
        state["content-type"] = w.headers.get("content-type")
        state["content-length"] = w.headers.get("content-length")
        state["vary"] = w.headers.get("vary")

    app.get("/", compressed)
    async with running_server(
        app,
        date=b"date: now\r\n",
        compression=CompressionConfig(
            min_size=0,
            brotli_level=-1,
            zstd_level=-1,
            gzip_level=6,
        ),
    ) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(
                b"GET / HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Accept-Encoding: gzip\r\n"
                b"Connection: close\r\n\r\n"
            )
            await writer.drain()
            payload = await read_response(reader)
            header, compressed_body = payload.split(b"\r\n\r\n", 1)
            assert b"x-custom: present\r\n" in header + b"\r\n"
            assert b"content-encoding: gzip\r\n" in header + b"\r\n"
            assert b"vary: origin, accept-encoding\r\n" in header + b"\r\n"
            assert b"content-type: text/plain; charset=utf-8\r\n" in header + b"\r\n"
            assert gzip.decompress(compressed_body) == body
            assert state == {
                "content-encoding": None,
                "content-type": None,
                "content-length": None,
                "vary": "origin",
            }
        finally:
            writer.close()
            await writer.wait_closed()


@pytest.mark.parametrize(
    ("accept_encoding", "expected"),
    [
        (b"gzip;q=1, br;q=0.5", b"gzip"),
        (b"gzip;q=0.5, br;q=0.5", b"br"),
        (b"BR;Q=1", b"br"),
        (b"*;q=0.8, identity;q=1", None),
    ],
)
@pytest.mark.asyncio
async def test_native_compression_qvalue_negotiation(
    accept_encoding: bytes,
    expected: bytes | None,
) -> None:
    app = App()
    body = b"compression negotiation " * 32

    async def compressed(_c, w):
        w.respond(body, b"text/plain")

    app.get("/", compressed)
    async with running_server(
        app,
        date=b"date: now\r\n",
        compression=CompressionConfig(
            min_size=0,
            brotli_level=4,
            zstd_level=-1,
            gzip_level=6,
        ),
    ) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(
                b"GET / HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Accept-Encoding: "
                + accept_encoding
                + b"\r\nConnection: keep-alive, CLOSE\r\n\r\n"
            )
            await writer.drain()
            payload = await read_response(reader)
            header = payload.split(b"\r\n\r\n", 1)[0] + b"\r\n"
            if expected is None:
                assert b"content-encoding:" not in header
            else:
                assert b"content-encoding: " + expected + b"\r\n" in header
        finally:
            writer.close()
            await writer.wait_closed()


@pytest.mark.parametrize(
    ("content_type", "compressed"),
    [
        (b" Text/Plain ; charset=utf-8", True),
        (b"IMAGE/PNG; charset=binary", False),
        (b"application/zip", False),
        (b"; charset=utf-8", False),
    ],
)
@pytest.mark.asyncio
async def test_native_content_type_compression_check(
    content_type: bytes,
    compressed: bool,
) -> None:
    app = App()

    async def respond(_c, w):
        w.respond(b"x" * 1024, content_type)

    app.get("/", respond)
    async with running_server(
        app,
        date=b"date: now\r\n",
        compression=CompressionConfig(
            min_size=0,
            brotli_level=-1,
            zstd_level=-1,
            gzip_level=6,
        ),
    ) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(
                b"GET / HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Accept-Encoding: gzip\r\n"
                b"Connection: close\r\n\r\n"
            )
            await writer.drain()
            header = (await read_response(reader)).split(b"\r\n\r\n", 1)[0]
            assert (b"content-encoding: gzip\r\n" in header + b"\r\n") is compressed
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
    async with running_server(
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
            compressed = [_read := await read_chunk(reader)]
            assert _read
            release_second.set()
            while chunk := await read_chunk(reader):
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
    async with running_server(
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
            first_chunk = await read_chunk(reader)
            assert first_chunk
            decoder = brotli.Decompressor()
            decoded = [decoder.process(first_chunk)]
            assert decoded == [b"data: 0\n\n"]

            release_second.set()
            while chunk := await read_chunk(reader):
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
    async with running_server(app, date=b"date: boxed\r\n") as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(
                b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
            payload = await read_response(reader)
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
    async with running_server(app, date=b"date: now\r\n") as port:
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
    async with running_server(app, date=b"date: now\r\n") as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(
                b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
            payload = await read_response(reader)
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
    async with running_server(app, date=b"date: now\r\n") as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(
                b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
            payload = await read_response(reader)
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
    async with running_server(app, date=b"date: now\r\n") as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(
                b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
            payload = await read_response(reader)
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
    async with running_server(
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
            assert await read_chunk(reader) == b"aaabbbccc"
            assert await read_chunk(reader) == b""
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
    async with running_server(
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
            payload = await read_response(reader)
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
    async with running_server(app, date=b"date: now\r\n") as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
            await writer.drain()
            async with asyncio.timeout(2):
                exc = await caught
            assert "bytes-like" in str(exc)
            await read_response(reader)
        finally:
            writer.close()
            await writer.wait_closed()
