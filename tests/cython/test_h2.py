"""HTTP/2 on the Cython protocol: cleartext prior knowledge and TLS ALPN."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from stario_cython.protocol import HttpProtocol

import stario.responses as responses
from stario import App
from stario.http.tls import load_tls_context
from stario.routing import UrlPath
from tests.cython.h2wire import (
    FLAG_END_HEADERS,
    FLAG_END_STREAM,
    NGHTTP2_CANCEL,
    NGHTTP2_PROTOCOL_ERROR,
    SETTINGS_HEADER_TABLE_SIZE,
    SETTINGS_MAX_HEADER_LIST_SIZE,
    SETTINGS_NO_RFC7540_PRIORITIES,
    TYPE_CONTINUATION,
    TYPE_DATA,
    TYPE_HEADERS,
    collected_settings,
    encode_request,
    h2_handshake,
    has_goaway,
    has_rst,
    headers_duplicate_method,
    headers_duplicate_path,
    pack_frame,
    parse_frames,
    read_stream,
    rst_code,
    stream_data,
    stream_ended,
    stream_headers_blob,
)
from tests.cython.http import RecordingTransport, free_port, make_protocol


async def _curl(*args: str) -> subprocess.CompletedProcess[str]:
    """Run curl without blocking the event loop (the server lives on it)."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        lambda: subprocess.run(
            ["curl", "-sS", "--max-time", "5", *args],
            check=False,
            capture_output=True,
            text=True,
        ),
    )


def _make_self_signed(dir_path: Path) -> tuple[Path, Path]:
    cert = dir_path / "cert.pem"
    key = dir_path / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-nodes",
            "-subj",
            "/CN=localhost",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return cert, key


@pytest.mark.asyncio
async def test_h2_prior_knowledge_plaintext() -> None:
    app = App()

    async def hello(c, w) -> None:
        assert c.req.protocol_version == "2"
        assert c.req.query.get("q") == "1"
        responses.text(w, f"h2:{c.req.path}")

    app.get("/hi", hello)
    loop = asyncio.get_running_loop()
    connections: set[HttpProtocol] = set()
    port = free_port()
    server = await loop.create_server(
        lambda: make_protocol(loop, app, connections=connections),
        "127.0.0.1",
        port,
    )
    try:
        result = await _curl(
            "--http2-prior-knowledge",
            f"http://127.0.0.1:{port}/hi?q=1",
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == "h2:/hi"
    finally:
        server.close()
        await server.wait_closed()
        await app.drain_tasks()


@pytest.mark.asyncio
async def test_h2_trailing_slash_redirects() -> None:
    """H2 trailing-slash 308 is frames, not HTTP/1.1 text (same rule as H1)."""
    app = App()

    async def search(c, w) -> None:
        responses.text(w, "nope")

    app.get("/search", search)
    loop = asyncio.get_running_loop()
    port = free_port()
    server = await loop.create_server(
        lambda: make_protocol(loop, app),
        "127.0.0.1",
        port,
    )
    try:
        result = await _curl(
            "--http2-prior-knowledge",
            "-D",
            "-",
            "-o",
            "/dev/null",
            f"http://127.0.0.1:{port}/search/?q=1",
        )
        assert result.returncode == 0, result.stderr
        assert "HTTP/2 308" in result.stdout
        assert "location: /search?q=1" in result.stdout.lower()
        assert "nope" not in result.stdout
    finally:
        server.close()
        await server.wait_closed()
        await app.drain_tasks()


@pytest.mark.asyncio
async def test_h2_post_empty_body() -> None:
    app = App()

    async def echo(c, w) -> None:
        body = await c.req.body()
        w.respond(body, b"text/plain; charset=utf-8", 200)

    app.post("/echo", echo)
    loop = asyncio.get_running_loop()
    port = free_port()
    server = await loop.create_server(
        lambda: make_protocol(loop, app),
        "127.0.0.1",
        port,
    )
    try:
        result = await _curl(
            "--http2-prior-knowledge",
            "-X",
            "POST",
            "-H",
            "Content-Length: 0",
            f"http://127.0.0.1:{port}/echo",
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == ""
    finally:
        server.close()
        await server.wait_closed()
        await app.drain_tasks()


@pytest.mark.asyncio
async def test_h2_post_large_body() -> None:
    """Bodies larger than 256KiB still dispatch at headers; body() waits on DATA."""
    payload = b"x" * (257 * 1024)
    app = App()

    async def echo(c, w) -> None:
        body = await c.req.body()
        w.respond(body, b"text/plain; charset=utf-8", 200)

    app.post("/echo", echo)
    loop = asyncio.get_running_loop()
    port = free_port()
    server = await loop.create_server(
        lambda: make_protocol(loop, app),
        "127.0.0.1",
        port,
    )
    try:
        with tempfile.NamedTemporaryFile() as tmp:
            tmp.write(payload)
            tmp.flush()
            result = await _curl(
                "--http2-prior-knowledge",
                "-X",
                "POST",
                "--data-binary",
                f"@{tmp.name}",
                f"http://127.0.0.1:{port}/echo",
            )
        assert result.returncode == 0, result.stderr
        assert result.stdout.encode("latin-1") == payload
    finally:
        server.close()
        await server.wait_closed()
        await app.drain_tasks()


def _iter_h2_frames(blob: bytes):
    """Yield (type, flags, stream_id, payload) from a wire dump."""
    i = 0
    while i + 9 <= len(blob):
        length = int.from_bytes(blob[i : i + 3], "big")
        end = i + 9 + length
        if end > len(blob):
            break
        yield (
            blob[i + 3],
            blob[i + 4],
            int.from_bytes(blob[i + 5 : i + 9], "big") & 0x7FFFFFFF,
            blob[i + 9 : end],
        )
        i = end


@pytest.mark.asyncio
async def test_h2_keep_alive_returns_window_credit() -> None:
    """Small POSTs on one connection must return recv credit when streams end.

    nghttp2 consume() only emits WINDOW_UPDATE at 50% of the local window
    (2MiB for a 4MiB connection). A client that only has ~1MiB of send
    credit then deadlocks. Credit must be submitted, not merely consumed.
    """
    if shutil.which("h2load") is None:
        pytest.skip("h2load not installed")
    payload = b"x" * 256
    app = App()

    async def echo(c, w) -> None:
        body = await c.req.body()
        w.respond(body, b"text/plain; charset=utf-8", 200)

    app.post("/echo", echo)
    loop = asyncio.get_running_loop()
    port = free_port()
    server = await loop.create_server(
        lambda: make_protocol(loop, app),
        "127.0.0.1",
        port,
    )
    nreq = 20_000
    try:
        with tempfile.NamedTemporaryFile() as tmp:
            tmp.write(payload)
            tmp.flush()
            result = await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    [
                        "h2load",
                        "-n",
                        str(nreq),
                        "-c",
                        "1",
                        "-m",
                        "1",
                        "-d",
                        tmp.name,
                        f"http://127.0.0.1:{port}/echo",
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                ),
            )
        assert result.returncode == 0, result.stderr
        assert f"{nreq} succeeded" in result.stdout, result.stdout
        assert " 0 failed" in result.stdout and " 0 errored" in result.stdout
    finally:
        server.close()
        await server.wait_closed()
        await app.drain_tasks()


@pytest.mark.asyncio
async def test_h2_post_body() -> None:
    app = App()

    async def echo(c, w) -> None:
        body = await c.req.body()
        w.respond(body, b"text/plain; charset=utf-8", 200)

    app.post("/echo", echo)
    loop = asyncio.get_running_loop()
    port = free_port()
    server = await loop.create_server(
        lambda: make_protocol(loop, app),
        "127.0.0.1",
        port,
    )
    try:
        result = await _curl(
            "--http2-prior-knowledge",
            "-X",
            "POST",
            "--data-binary",
            "abcde",
            f"http://127.0.0.1:{port}/echo",
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == "abcde"
    finally:
        server.close()
        await server.wait_closed()
        await app.drain_tasks()


@pytest.mark.asyncio
async def test_tls_http11_and_h2_alpn() -> None:
    app = App()

    async def hello(c, w) -> None:
        responses.text(w, f"v={c.req.protocol_version}")

    app.get("/tls", hello)
    loop = asyncio.get_running_loop()
    with tempfile.TemporaryDirectory() as tmp:
        cert, key = _make_self_signed(Path(tmp))
        ctx = load_tls_context(certfile=cert, keyfile=key)
        port = free_port()
        server = await loop.create_server(
            lambda: make_protocol(loop, app),
            "127.0.0.1",
            port,
            ssl=ctx,
        )
        try:
            h1 = await _curl(
                "--http1.1",
                "-k",
                f"https://127.0.0.1:{port}/tls",
            )
            assert h1.returncode == 0, h1.stderr
            assert h1.stdout == "v=1.1"

            h2 = await _curl(
                "--http2",
                "-k",
                f"https://127.0.0.1:{port}/tls",
            )
            assert h2.returncode == 0, h2.stderr
            assert h2.stdout == "v=2"
        finally:
            server.close()
            await server.wait_closed()
            await app.drain_tasks()


@pytest.mark.asyncio
async def test_h2_connect_is_rejected() -> None:
    """No tunnels or WebSockets: CONNECT must not dispatch a handler."""
    app = App()
    seen: list[str] = []

    async def boom(c, w) -> None:
        seen.append(c.req.method)
        responses.text(w, "nope")

    app.get("/", boom)
    loop = asyncio.get_running_loop()
    port = free_port()
    server = await loop.create_server(
        lambda: make_protocol(loop, app),
        "127.0.0.1",
        port,
    )
    try:
        result = await _curl(
            "--http2-prior-knowledge",
            "-X",
            "CONNECT",
            f"http://127.0.0.1:{port}/",
        )
        assert seen == []
        assert "nope" not in result.stdout
    finally:
        server.close()
        await server.wait_closed()
        await app.drain_tasks()


@pytest.mark.asyncio
async def test_h2_preface_on_recording_transport_does_not_raise() -> None:
    loop = asyncio.get_running_loop()
    app = App()
    proto = make_protocol(loop, app)
    transport = RecordingTransport(proto)
    proto.connection_made(transport)
    try:
        proto.data_received(b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n")
        assert proto.parse_mode == 2
        assert transport.writes  # server SETTINGS
        wire = b"".join(transport.writes)
        conn_updates = [
            int.from_bytes(payload, "big") & 0x7FFFFFFF
            for ftype, _flags, stream_id, payload in _iter_h2_frames(wire)
            if ftype == 0x8 and stream_id == 0 and len(payload) == 4
        ]
        # Connection window starts at 65535; we enlarge it to 4MiB.
        assert conn_updates
        assert sum(conn_updates) >= (4 * 1024 * 1024) - 65535
        frames, _rest = parse_frames(wire)
        settings = collected_settings(frames)
        assert settings.get(SETTINGS_MAX_HEADER_LIST_SIZE) == 64 * 1024
        assert settings.get(SETTINGS_NO_RFC7540_PRIORITIES) == 1
        assert settings.get(SETTINGS_HEADER_TABLE_SIZE) == 0
    finally:
        proto.connection_lost(None)
        await app.drain_tasks()


def _h2_oversize_rejected(frames: list, stream_id: int, hits: int) -> None:
    """Oversize H2 headers: no handler, this stream dies, connection stays up."""
    assert hits == 0
    assert not has_goaway(frames)
    assert stream_ended(frames, stream_id)
    blob = stream_headers_blob(frames, stream_id)
    assert b"431" in blob or has_rst(frames, stream_id)


@pytest.mark.asyncio
async def test_h2_settings_advertise_header_budget() -> None:
    loop = asyncio.get_running_loop()
    app = App()
    proto = make_protocol(loop, app, max_header_bytes=512)
    transport = RecordingTransport(proto)
    proto.connection_made(transport)
    try:
        proto.data_received(b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n")
        frames, _rest = parse_frames(b"".join(transport.writes))
        settings = collected_settings(frames)
        assert settings.get(SETTINGS_MAX_HEADER_LIST_SIZE) == 512
        assert settings.get(SETTINGS_NO_RFC7540_PRIORITIES) == 1
        assert settings.get(SETTINGS_HEADER_TABLE_SIZE) == 0
    finally:
        proto.connection_lost(None)
        await app.drain_tasks()


@pytest.mark.asyncio
async def test_h2_small_get_under_header_budget() -> None:
    app = App()
    hits = 0

    async def hello(_c, w) -> None:
        nonlocal hits
        hits += 1
        responses.text(w, "ok")

    app.get("/", hello)
    loop = asyncio.get_running_loop()
    port = free_port()
    server = await loop.create_server(
        lambda: make_protocol(loop, app, max_header_bytes=512),
        "127.0.0.1",
        port,
    )
    try:
        reader, writer, buf = await h2_handshake("127.0.0.1", port)
        try:
            writer.write(
                pack_frame(
                    TYPE_HEADERS,
                    FLAG_END_HEADERS | FLAG_END_STREAM,
                    1,
                    encode_request(path="/"),
                )
            )
            await writer.drain()
            frames, _buf = await read_stream(reader, buf, 1)
            assert not has_rst(frames, 1)
            assert not has_goaway(frames)
            assert stream_data(frames, 1) == b"ok"
            assert hits == 1
        finally:
            writer.close()
            await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()
        await app.drain_tasks()


@pytest.mark.asyncio
async def test_h2_header_over_limit_returns_431_without_dispatch() -> None:
    app = App()
    hits = 0

    async def hello(_c, w) -> None:
        nonlocal hits
        hits += 1
        responses.text(w, "should not run")

    app.get("/", hello)
    loop = asyncio.get_running_loop()
    port = free_port()
    server = await loop.create_server(
        lambda: make_protocol(loop, app, max_header_bytes=512),
        "127.0.0.1",
        port,
    )
    try:
        result = await _curl(
            "--http2-prior-knowledge",
            "-D",
            "-",
            "-o",
            "/dev/null",
            "-H",
            "X-Pad: " + ("x" * 600),
            f"http://127.0.0.1:{port}/",
        )
        assert result.returncode == 0, result.stderr
        assert "HTTP/2 431" in result.stdout
        assert "should not run" not in result.stdout
        assert hits == 0
    finally:
        server.close()
        await server.wait_closed()
        await app.drain_tasks()


@pytest.mark.asyncio
async def test_h2_oversize_headers_do_not_kill_connection() -> None:
    """431 is per-stream: a later GET on the same connection still runs."""
    app = App()
    hits = 0

    async def hello(_c, w) -> None:
        nonlocal hits
        hits += 1
        responses.text(w, "ok")

    app.get("/", hello)
    loop = asyncio.get_running_loop()
    port = free_port()
    server = await loop.create_server(
        lambda: make_protocol(loop, app, max_header_bytes=512),
        "127.0.0.1",
        port,
    )
    try:
        reader, writer, buf = await h2_handshake("127.0.0.1", port)
        try:
            writer.write(
                pack_frame(
                    TYPE_HEADERS,
                    FLAG_END_HEADERS | FLAG_END_STREAM,
                    1,
                    encode_request(
                        path="/",
                        extra=[(b"x-pad", b"x" * 600)],
                    ),
                )
            )
            await writer.drain()
            frames, buf = await read_stream(reader, buf, 1)
            _h2_oversize_rejected(frames, 1, hits)

            writer.write(
                pack_frame(
                    TYPE_HEADERS,
                    FLAG_END_HEADERS | FLAG_END_STREAM,
                    3,
                    encode_request(path="/"),
                )
            )
            await writer.drain()
            frames, _buf = await read_stream(reader, buf, 3)
            assert not has_rst(frames, 3)
            assert not has_goaway(frames)
            assert stream_data(frames, 3) == b"ok"
            assert hits == 1
        finally:
            writer.close()
            await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()
        await app.drain_tasks()


@pytest.mark.asyncio
async def test_h2_oversize_path_returns_431() -> None:
    app = App()
    hits = 0

    async def hello(_c, w) -> None:
        nonlocal hits
        hits += 1
        responses.text(w, "should not run")

    app.get("/{path...}", hello)
    loop = asyncio.get_running_loop()
    port = free_port()
    server = await loop.create_server(
        lambda: make_protocol(loop, app, max_header_bytes=512),
        "127.0.0.1",
        port,
    )
    try:
        reader, writer, buf = await h2_handshake("127.0.0.1", port)
        try:
            writer.write(
                pack_frame(
                    TYPE_HEADERS,
                    FLAG_END_HEADERS | FLAG_END_STREAM,
                    1,
                    encode_request(path="/" + ("a" * 600)),
                )
            )
            await writer.drain()
            frames, _buf = await read_stream(reader, buf, 1)
            _h2_oversize_rejected(frames, 1, hits)
        finally:
            writer.close()
            await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()
        await app.drain_tasks()


@pytest.mark.asyncio
async def test_h2_declared_body_over_limit_returns_413() -> None:
    app = App()
    hits = 0

    async def hello(_c, w) -> None:
        nonlocal hits
        hits += 1
        responses.text(w, "should not run")

    app.post("/", hello)
    loop = asyncio.get_running_loop()
    port = free_port()
    server = await loop.create_server(
        lambda: make_protocol(loop, app, max_body_bytes=20),
        "127.0.0.1",
        port,
    )
    try:
        result = await _curl(
            "--http2-prior-knowledge",
            "-D",
            "-",
            "-o",
            "/dev/null",
            "-X",
            "POST",
            "-d",
            "x" * 100,
            f"http://127.0.0.1:{port}/",
        )
        assert result.returncode == 0, result.stderr
        assert "HTTP/2 413" in result.stdout
        assert "should not run" not in result.stdout
        assert hits == 0
    finally:
        server.close()
        await server.wait_closed()
        await app.drain_tasks()


@pytest.mark.asyncio
async def test_h2_oversize_body_does_not_kill_connection() -> None:
    app = App()
    hits = 0

    async def hello(_c, w) -> None:
        nonlocal hits
        hits += 1
        responses.text(w, "ok")

    app.get("/", hello)
    app.post("/", hello)
    loop = asyncio.get_running_loop()
    port = free_port()
    server = await loop.create_server(
        lambda: make_protocol(loop, app, max_body_bytes=20),
        "127.0.0.1",
        port,
    )
    try:
        reader, writer, buf = await h2_handshake("127.0.0.1", port)
        try:
            writer.write(
                pack_frame(
                    TYPE_HEADERS,
                    FLAG_END_HEADERS,
                    1,
                    encode_request(
                        method="POST",
                        path="/",
                        extra=[(b"content-length", b"100")],
                    ),
                )
            )
            await writer.drain()
            frames, buf = await read_stream(reader, buf, 1)
            assert hits == 0
            assert not has_goaway(frames)
            blob = stream_headers_blob(frames, 1)
            assert b"413" in blob or has_rst(frames, 1)

            writer.write(
                pack_frame(
                    TYPE_HEADERS,
                    FLAG_END_HEADERS | FLAG_END_STREAM,
                    3,
                    encode_request(path="/"),
                )
            )
            await writer.drain()
            frames, _buf = await read_stream(reader, buf, 3)
            assert not has_rst(frames, 3)
            assert not has_goaway(frames)
            assert stream_data(frames, 3) == b"ok"
            assert hits == 1
        finally:
            writer.close()
            await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()
        await app.drain_tasks()


@pytest.mark.asyncio
async def test_h2_incomplete_headers_timeout_does_not_kill_connection() -> None:
    """A stalled HEADERS block is RST on that stream; a later GET still runs."""
    app = App()
    hits = 0

    async def hello(_c, w) -> None:
        nonlocal hits
        hits += 1
        responses.text(w, "ok")

    app.get("/", hello)
    loop = asyncio.get_running_loop()
    connections: set[HttpProtocol] = set()
    port = free_port()
    server = await loop.create_server(
        lambda: make_protocol(loop, app, connections=connections, header_timeout=0.15),
        "127.0.0.1",
        port,
    )
    try:
        reader, writer, buf = await h2_handshake("127.0.0.1", port)
        try:
            writer.write(
                pack_frame(
                    TYPE_HEADERS,
                    0,
                    1,
                    encode_request(path="/"),
                )
            )
            await writer.drain()
            frames, buf = await read_stream(reader, buf, 1, timeout=2.0)
            assert hits == 0
            assert not has_goaway(frames)
            assert has_rst(frames, 1)
            code = rst_code(frames, 1)
            if code is not None:
                assert code == NGHTTP2_CANCEL

            # Header block is still open until END_HEADERS (RFC 9113). RST
            # does not end it; a CONTINUATION must follow before stream 3.
            writer.write(
                pack_frame(TYPE_CONTINUATION, FLAG_END_HEADERS, 1)
                + pack_frame(
                    TYPE_HEADERS,
                    FLAG_END_HEADERS | FLAG_END_STREAM,
                    3,
                    encode_request(path="/"),
                )
            )
            await writer.drain()
            frames, _buf = await read_stream(reader, buf, 3)
            assert not has_rst(frames, 3)
            assert stream_data(frames, 3) == b"ok"
            assert hits == 1
        finally:
            writer.close()
            await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()
        await app.drain_tasks()


@pytest.mark.asyncio
async def test_h2_post_without_content_length_reads_data() -> None:
    """HEADERS without END_STREAM and no Content-Length still arms the body."""
    app = App()
    seen: list[bytes] = []

    async def echo(c, w) -> None:
        body = await c.req.body()
        seen.append(body)
        w.respond(body, b"text/plain; charset=utf-8", 200)

    app.post("/echo", echo)
    loop = asyncio.get_running_loop()
    port = free_port()
    server = await loop.create_server(
        lambda: make_protocol(loop, app),
        "127.0.0.1",
        port,
    )
    try:
        reader, writer, buf = await h2_handshake("127.0.0.1", port)
        try:
            payload = b"no-cl-body"
            writer.write(
                pack_frame(
                    TYPE_HEADERS,
                    FLAG_END_HEADERS,
                    1,
                    encode_request(method="POST", path="/echo"),
                )
                + pack_frame(TYPE_DATA, FLAG_END_STREAM, 1, payload)
            )
            await writer.drain()
            frames, _buf = await read_stream(reader, buf, 1)
            assert not has_rst(frames, 1)
            assert stream_data(frames, 1) == payload
            assert seen == [payload]
        finally:
            writer.close()
            await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()
        await app.drain_tasks()


@pytest.mark.asyncio
async def test_h2_post_without_content_length_keep_alive_isolates_bodies() -> None:
    """Recycled exchanges must not ingest DATA from a later stream."""
    app = App()
    seen: list[bytes] = []

    async def echo(c, w) -> None:
        body = await c.req.body()
        seen.append(body)
        w.respond(body, b"text/plain; charset=utf-8", 200)

    app.post("/echo", echo)
    loop = asyncio.get_running_loop()
    port = free_port()
    server = await loop.create_server(
        lambda: make_protocol(loop, app),
        "127.0.0.1",
        port,
    )
    try:
        reader, writer, buf = await h2_handshake("127.0.0.1", port)
        try:
            for stream_id, payload in ((1, b"aaa"), (3, b"bbb")):
                writer.write(
                    pack_frame(
                        TYPE_HEADERS,
                        FLAG_END_HEADERS,
                        stream_id,
                        encode_request(method="POST", path="/echo"),
                    )
                    + pack_frame(TYPE_DATA, FLAG_END_STREAM, stream_id, payload)
                )
                await writer.drain()
                frames, buf = await read_stream(reader, buf, stream_id)
                assert not has_rst(frames, stream_id)
                assert stream_data(frames, stream_id) == payload
            assert seen == [b"aaa", b"bbb"]
        finally:
            writer.close()
            await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()
        await app.drain_tasks()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [headers_duplicate_path(), headers_duplicate_method()],
    ids=["path", "method"],
)
async def test_h2_duplicate_pseudo_header_is_rejected(headers: bytes) -> None:
    app = App()
    seen: list[str] = []

    async def boom(c, w) -> None:
        seen.append(c.req.method)
        responses.text(w, "nope")

    app.get("/", boom)
    app.get("/other", boom)
    loop = asyncio.get_running_loop()
    port = free_port()
    server = await loop.create_server(
        lambda: make_protocol(loop, app),
        "127.0.0.1",
        port,
    )
    try:
        reader, writer, buf = await h2_handshake("127.0.0.1", port)
        try:
            writer.write(
                pack_frame(
                    TYPE_HEADERS,
                    FLAG_END_HEADERS | FLAG_END_STREAM,
                    1,
                    headers,
                )
            )
            await writer.drain()
            frames, _buf = await read_stream(reader, buf, 1)
            assert has_rst(frames, 1) or has_goaway(frames)
            code = rst_code(frames, 1)
            if code is not None:
                assert code == NGHTTP2_PROTOCOL_ERROR
            assert seen == []
        finally:
            writer.close()
            await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()
        await app.drain_tasks()


@pytest.mark.asyncio
async def test_h2_authority_host_mismatch_is_rejected() -> None:
    app = App()
    seen: list[str] = []

    async def boom(c, w) -> None:
        seen.append(c.req.host)
        responses.text(w, "nope")

    app.get("/", boom)
    loop = asyncio.get_running_loop()
    port = free_port()
    server = await loop.create_server(
        lambda: make_protocol(loop, app),
        "127.0.0.1",
        port,
    )
    try:
        reader, writer, buf = await h2_handshake("127.0.0.1", port)
        try:
            writer.write(
                pack_frame(
                    TYPE_HEADERS,
                    FLAG_END_HEADERS | FLAG_END_STREAM,
                    1,
                    encode_request(
                        path="/",
                        authority="example.com",
                        extra=[(b"host", b"other.com")],
                    ),
                )
            )
            await writer.drain()
            frames, _buf = await read_stream(reader, buf, 1)
            assert has_rst(frames, 1) or has_goaway(frames)
            assert seen == []
        finally:
            writer.close()
            await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()
        await app.drain_tasks()


@pytest.mark.asyncio
async def test_h2_authority_host_match_keeps_one_host() -> None:
    app = App()
    seen: list[tuple[str, list[str]]] = []

    async def hello(c, w) -> None:
        seen.append((c.req.host, c.req.headers.getlist("host")))
        responses.text(w, c.req.host)

    app.get("/", hello)
    loop = asyncio.get_running_loop()
    port = free_port()
    server = await loop.create_server(
        lambda: make_protocol(loop, app),
        "127.0.0.1",
        port,
    )
    try:
        reader, writer, buf = await h2_handshake("127.0.0.1", port)
        try:
            writer.write(
                pack_frame(
                    TYPE_HEADERS,
                    FLAG_END_HEADERS | FLAG_END_STREAM,
                    1,
                    encode_request(
                        path="/",
                        authority="example.com",
                        extra=[(b"host", b"example.com")],
                    ),
                )
            )
            await writer.drain()
            frames, _buf = await read_stream(reader, buf, 1)
            assert not has_rst(frames, 1)
            assert stream_data(frames, 1) == b"example.com"
            assert seen == [("example.com", ["example.com"])]
        finally:
            writer.close()
            await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()
        await app.drain_tasks()


@pytest.mark.asyncio
async def test_h2_host_routing_normalizes_authority() -> None:
    app = App()

    async def hello(c, w) -> None:
        responses.text(w, f"host:{c.req.host}")

    app.get(UrlPath("/", host="example.com"), hello)
    loop = asyncio.get_running_loop()
    port = free_port()
    server = await loop.create_server(
        lambda: make_protocol(loop, app),
        "127.0.0.1",
        port,
    )
    try:
        reader, writer, buf = await h2_handshake("127.0.0.1", port)
        try:
            writer.write(
                pack_frame(
                    TYPE_HEADERS,
                    FLAG_END_HEADERS | FLAG_END_STREAM,
                    1,
                    encode_request(path="/", authority="Example.COM:80"),
                )
            )
            await writer.drain()
            frames, _buf = await read_stream(reader, buf, 1)
            assert not has_rst(frames, 1)
            assert stream_data(frames, 1) == b"host:example.com"
        finally:
            writer.close()
            await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()
        await app.drain_tasks()
