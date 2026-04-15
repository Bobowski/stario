"""HTTP protocol edge cases and bounded random input (smoke fuzz).

These tests do **not** replace dedicated fuzzers (AFL++/libFuzzer) or external audits; they
regress common parser failure modes and ensure arbitrary bytes do not escape as unhandled
exceptions from the protocol layer. See ``SECURITY.md`` for scope and limitations.
"""

import asyncio
import random
from io import StringIO
from typing import Any

import pytest

from stario import App
from stario.http.protocol import HttpProtocol
from stario.http.writer import CompressionConfig
from stario.telemetry import JsonTracer


class _RecordingTransport(asyncio.Transport):
    """Minimal ``asyncio.Transport`` surface used by ``HttpProtocol``."""

    __slots__ = ("_protocol", "writes", "_closing")

    def __init__(self, protocol: asyncio.Protocol) -> None:
        super().__init__()
        self._protocol = protocol
        self.writes: list[bytes] = []
        self._closing = False

    def write(self, data: bytes | bytearray | memoryview[Any]) -> None:
        self.writes.append(bytes(data))

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._protocol.connection_lost(None)

    def is_closing(self) -> bool:
        return self._closing

    def pause_reading(self) -> None:
        pass

    def resume_reading(self) -> None:
        pass


def _make_protocol(
    *,
    max_request_header_bytes: int = 16_384,
    max_request_body_bytes: int = 16_384,
    app: App | None = None,
) -> tuple[HttpProtocol, App, _RecordingTransport]:
    loop = asyncio.get_running_loop()
    shutdown = loop.create_future()
    connections: set[HttpProtocol] = set()
    if app is None:
        app = App()
    tracer = JsonTracer(StringIO())
    proto = HttpProtocol(
        loop,
        app,
        tracer,
        lambda: b"date: Thu, 01 Jan 1970 00:00:00 GMT\r\n",
        CompressionConfig(),
        shutdown,
        connections,
        max_request_header_bytes=max_request_header_bytes,
        max_request_body_bytes=max_request_body_bytes,
    )
    transport = _RecordingTransport(proto)
    proto.connection_made(transport)
    return proto, app, transport


def _response_status(transport: _RecordingTransport) -> int | None:
    raw = b"".join(transport.writes)
    if not raw.startswith(b"HTTP/"):
        return None
    line = raw.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
    parts = line.split(None, 2)
    if len(parts) < 2:
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


async def _drain(app: App) -> None:
    await asyncio.sleep(0)
    await app.join_tasks()


@pytest.mark.asyncio
async def test_garbage_bytes_yield_400_or_close_without_exception() -> None:
    proto, app, transport = _make_protocol()
    try:
        proto.data_received(b"\x00\xff\xfe not http \r\n\r\n")
        await _drain(app)
        status = _response_status(transport)
        assert status == 400
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_upgrade_request_yields_400() -> None:
    proto, app, transport = _make_protocol()
    try:
        proto.data_received(
            b"GET / HTTP/1.1\r\n"
            b"Host: t\r\n"
            b"Connection: Upgrade\r\n"
            b"Upgrade: websocket\r\n"
            b"\r\n"
        )
        await _drain(app)
        assert _response_status(transport) == 400
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_valid_get_reaches_app_and_returns_404() -> None:
    proto, app, transport = _make_protocol()
    try:
        proto.data_received(b"GET / HTTP/1.1\r\nHost: t\r\n\r\n")
        await _drain(app)
        assert _response_status(transport) == 404
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_header_total_over_limit_returns_431() -> None:
    limit = 512
    proto, app, transport = _make_protocol(max_request_header_bytes=limit)
    try:
        # Large header name+value pushes cumulative head bytes past ``limit``.
        pad = b"x" * (limit + 32)
        proto.data_received(
            b"GET / HTTP/1.1\r\nHost: t\r\nX-Pad: " + pad + b"\r\n\r\n"
        )
        await _drain(app)
        assert _response_status(transport) == 431
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_body_over_limit_returns_413() -> None:
    app = App()

    async def read_body(c, w) -> None:
        await c.req.body()
        from stario import responses

        responses.text(w, "ok")

    app.post("/", read_body)

    proto, app, transport = _make_protocol(
        app=app,
        max_request_header_bytes=8192,
        max_request_body_bytes=20,
    )
    try:
        proto.data_received(
            b"POST / HTTP/1.1\r\n"
            b"Host: t\r\n"
            b"Content-Length: 100\r\n"
            b"\r\n" + (b"y" * 100)
        )
        await _drain(app)
        assert _response_status(transport) == 413
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_random_inputs_do_not_raise() -> None:
    seed, rounds = 42, 300
    rng = random.Random(seed)

    for i in range(rounds):
        proto, app, transport = _make_protocol()
        try:
            n = rng.randint(0, 512)
            payload = rng.randbytes(n) if hasattr(rng, "randbytes") else bytes(
                rng.getrandbits(8) for _ in range(n)
            )
            proto.data_received(payload)
            await _drain(app)
        finally:
            if not transport.is_closing():
                transport.close()
            await _drain(app)

        assert i >= 0  # loop body must complete; exceptions fail the test
