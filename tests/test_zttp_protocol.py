"""Python protocol driven by zttp instead of httptools."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("zttp")

import stario.responses as responses
from stario import App
from stario.http.compression import CompressionConfig
from stario.http.config import RequestPolicy
from stario.http.zttp_protocol import HttpProtocol
from stario.telemetry.noop import NoOpTracer
from tests.test_http_protocol_hardening import _RecordingTransport, _response_status


def _attach(app: App | None = None) -> tuple[HttpProtocol, App, _RecordingTransport]:
    loop = asyncio.get_running_loop()
    if app is None:
        app = App()
    proto = HttpProtocol(
        loop,
        app,
        NoOpTracer(),
        lambda: b"date: Thu, 01 Jan 1970 00:00:00 GMT\r\n",
        CompressionConfig(),
        set(),
        RequestPolicy(),
    )
    transport = _RecordingTransport(proto)
    proto.connection_made(transport)
    return proto, app, transport


async def _drain(app: App) -> None:
    await asyncio.sleep(0)
    await app.drain_tasks()


@pytest.mark.asyncio
async def test_zttp_plaintext_and_keepalive_post() -> None:
    app = App()
    seen: list[str] = []

    async def hello(_c, w):
        seen.append("get")
        responses.text(w, "hi")

    async def echo(c, w):
        seen.append((await c.req.body()).decode())
        responses.text(w, "ok")

    app.get("/", hello)
    app.post("/echo", echo)
    proto, app, transport = _attach(app)
    try:
        proto.data_received(b"GET / HTTP/1.1\r\nHost: t\r\n\r\n")
        await _drain(app)
        assert _response_status(transport) == 200
        proto.data_received(
            b"POST /echo HTTP/1.1\r\n"
            b"Host: t\r\n"
            b"Content-Length: 3\r\n"
            b"\r\n"
            b"abc"
        )
        await _drain(app)
        assert seen == ["get", "abc"]
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_zttp_garbage_is_400() -> None:
    proto, app, transport = _attach()
    try:
        proto.data_received(b"\x00\xff not http \r\n\r\n")
        await _drain(app)
        assert _response_status(transport) == 400
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)
