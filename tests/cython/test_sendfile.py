"""w.sendfile(): OS sendfile on HTTP/1 cleartext, copy fallback elsewhere."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from stario import App, Files, Route
from stario.exceptions import StarioError, StarioRuntime
from tests.cython import h2wire as h2
from tests.cython.http import (
    RecordingTransport,
    make_protocol,
    read_response,
    running_server,
)


def _payload(n: int = 64 * 1024) -> bytes:
    return bytes(i & 0xFF for i in range(n))


@contextmanager
def _track_sendfile(
    loop: asyncio.AbstractEventLoop,
) -> Iterator[list[tuple[int, int | None]]]:
    real = loop.sendfile
    calls: list[tuple[int, int | None]] = []

    async def wrapped(transport, file, offset=0, count=None, *, fallback=True):
        calls.append((offset, count))
        return await real(transport, file, offset, count, fallback=fallback)

    loop.sendfile = wrapped  # type: ignore[method-assign]
    try:
        yield calls
    finally:
        loop.sendfile = real  # type: ignore[method-assign]


def _file_handler(path: Path, *, offset: int = 0, count: int | None = None):
    size = path.stat().st_size
    length = size if count is None else count

    async def send(_c, w) -> None:
        fd = os.open(path, os.O_RDONLY)
        try:
            w.headers.unsafe_set(b"content-type", b"application/octet-stream")
            w.headers.unsafe_set(b"content-length", b"%d" % length)
            w.write_headers(200)
            await w.sendfile(fd, offset, length)
            w.end()
        finally:
            os.close(fd)

    return send


@pytest.mark.asyncio
async def test_http1_sendfile_uses_the_loop_and_delivers_the_file(
    tmp_path: Path,
) -> None:
    payload = _payload()
    path = tmp_path / "blob.bin"
    path.write_bytes(payload)
    app = App()
    app.add(Route("GET /file"), _file_handler(path))
    loop = asyncio.get_running_loop()

    with _track_sendfile(loop) as calls:
        async with running_server(app) as port:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"GET /file HTTP/1.1\r\nHost: t\r\n\r\n")
            raw = await read_response(reader)
            writer.close()

    assert calls == [(0, len(payload))]
    assert raw.endswith(payload)
    assert b"content-length: %d" % len(payload) in raw.lower()


@pytest.mark.asyncio
async def test_http1_sendfile_range_and_keep_alive(tmp_path: Path) -> None:
    payload = _payload(4096)
    path = tmp_path / "blob.bin"
    path.write_bytes(payload)
    app = App()
    app.add(Route("GET /part"), _file_handler(path, offset=100, count=50))
    app.add(Route("GET /full"), _file_handler(path))
    loop = asyncio.get_running_loop()

    with _track_sendfile(loop) as calls:
        async with running_server(app) as port:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"GET /part HTTP/1.1\r\nHost: t\r\n\r\n")
            first = await read_response(reader)
            writer.write(b"GET /full HTTP/1.1\r\nHost: t\r\n\r\n")
            second = await read_response(reader)
            writer.close()

    assert calls == [(100, 50), (0, len(payload))]
    assert first.endswith(payload[100:150])
    assert second.endswith(payload)


@pytest.mark.asyncio
async def test_files_streamed_body_uses_sendfile(tmp_path: Path) -> None:
    payload = _payload(8000)
    (tmp_path / "big.bin").write_bytes(payload)
    files = Files(tmp_path)
    app = App()
    files.register(app)
    await files.load(max_file_size=1)
    loop = asyncio.get_running_loop()

    with _track_sendfile(loop) as calls:
        async with running_server(app) as port:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"GET /data/big.bin HTTP/1.1\r\nHost: t\r\n\r\n")
            raw = await read_response(reader)
            writer.write(
                b"GET /data/big.bin HTTP/1.1\r\nHost: t\r\nRange: bytes=10-19\r\n\r\n"
            )
            partial = await read_response(reader)
            writer.close()

    assert files.stats["streamed_files"] == 1
    assert calls == [(0, len(payload)), (10, 10)]
    assert raw.endswith(payload)
    assert partial.startswith(b"HTTP/1.1 206")
    assert partial.endswith(payload[10:20])


@pytest.mark.asyncio
async def test_http2_sendfile_does_not_dump_raw_bytes(tmp_path: Path) -> None:
    payload = _payload(1024)
    path = tmp_path / "blob.bin"
    path.write_bytes(payload)
    app = App()
    app.add(Route("GET /file"), _file_handler(path))
    loop = asyncio.get_running_loop()

    with _track_sendfile(loop) as calls:
        async with running_server(app) as port:
            reader, writer, buf = await h2.h2_handshake("127.0.0.1", port)
            writer.write(
                h2.pack_frame(
                    h2.TYPE_HEADERS,
                    h2.FLAG_END_HEADERS | h2.FLAG_END_STREAM,
                    1,
                    h2.encode_request(path="/file"),
                )
            )
            frames, _buf = await h2.read_stream(reader, buf, 1)
            writer.close()

    assert calls == []
    assert h2.stream_data(frames, 1) == payload


@pytest.mark.asyncio
async def test_sendfile_falls_back_when_the_transport_cannot(
    tmp_path: Path,
) -> None:
    payload = b"fallback-body"
    path = tmp_path / "blob.bin"
    path.write_bytes(payload)
    app = App()
    app.add(Route("GET /file"), _file_handler(path))
    loop = asyncio.get_running_loop()
    proto = make_protocol(loop, app)
    transport = RecordingTransport(proto)
    proto.connection_made(transport)
    try:
        proto.data_received(b"GET /file HTTP/1.1\r\nHost: t\r\n\r\n")
        await app.drain_tasks()
    finally:
        if not transport.is_closing():
            transport.close()
        await app.drain_tasks()

    assert payload in b"".join(transport.writes)


@pytest.mark.asyncio
async def test_sendfile_head_does_not_read_the_file(tmp_path: Path) -> None:
    path = tmp_path / "blob.bin"
    path.write_bytes(b"secret")
    app = App()
    app.add(Route("HEAD /file"), _file_handler(path))
    loop = asyncio.get_running_loop()

    with _track_sendfile(loop) as calls:
        async with running_server(app) as port:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"HEAD /file HTTP/1.1\r\nHost: t\r\n\r\n")
            async with asyncio.timeout(2):
                raw = await reader.readuntil(b"\r\n\r\n")
            writer.close()

    assert calls == []
    assert raw.startswith(b"HTTP/1.1 200")
    assert b"content-length: 6" in raw.lower()
    assert b"secret" not in raw


@pytest.mark.asyncio
async def test_sendfile_rejects_oversize_and_finished_handle(
    tmp_path: Path,
) -> None:
    path = tmp_path / "blob.bin"
    path.write_bytes(b"abcd")
    app = App()
    errors: list[str] = []

    async def bad(_c, w) -> None:
        fd = os.open(path, os.O_RDONLY)
        try:
            w.headers.unsafe_set(b"content-length", b"2")
            w.write_headers(200)
            try:
                await w.sendfile(fd, 0, 4)
            except StarioRuntime as exc:
                errors.append(str(exc))
            w.write(b"ab")
            w.end()
        finally:
            os.close(fd)

    async def late(_c, w) -> None:
        kept.append(w)
        w.headers.unsafe_set(b"content-length", b"2")
        w.write_headers(200)
        w.end(b"ok")

    kept: list = []
    app.add(Route("GET /bad"), bad)
    app.add(Route("GET /late"), late)

    async with running_server(app) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /bad HTTP/1.1\r\nHost: t\r\n\r\n")
        await read_response(reader)
        writer.write(b"GET /late HTTP/1.1\r\nHost: t\r\n\r\n")
        await read_response(reader)
        writer.close()

    assert any("Content-Length" in item for item in errors)
    with pytest.raises(StarioRuntime, match="handler already returned"):
        await kept[0].sendfile(0, 0, 1)


@pytest.mark.asyncio
async def test_sendfile_rejects_non_integers(tmp_path: Path) -> None:
    path = tmp_path / "blob.bin"
    path.write_bytes(b"x")
    app = App()
    seen: list[type[BaseException]] = []

    async def bad(_c, w) -> None:
        fd = os.open(path, os.O_RDONLY)
        try:
            try:
                await w.sendfile(fd, 0, True)  # type: ignore[arg-type]
            except StarioError as exc:
                seen.append(type(exc))
            w.write_headers(204)
            w.end()
        finally:
            os.close(fd)

    app.add(Route("GET /"), bad)
    async with running_server(app) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET / HTTP/1.1\r\nHost: t\r\n\r\n")
        await read_response(reader)
        writer.close()

    assert seen == [StarioError]
