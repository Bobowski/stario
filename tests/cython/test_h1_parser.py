"""Complete-message C parser vs llhttp fallback."""

from __future__ import annotations

import asyncio

import pytest

import stario.responses as responses
from stario import App
from stario_cython.protocol import HttpProtocol
from tests.cython.http import (
    RecordingTransport,
    make_protocol,
    response_status,
)


async def _drain(app: App) -> None:
    await asyncio.sleep(0)
    await app.drain_tasks()


def _attach(app: App | None = None):
    loop = asyncio.get_running_loop()
    if app is None:
        app = App()
    proto = make_protocol(loop, app)
    transport = RecordingTransport(proto)
    proto.connection_made(transport)
    return proto, app, transport


@pytest.mark.asyncio
async def test_wrk_style_get_uses_complete_message_path() -> None:
    app = App()
    hits: list[str] = []

    async def handler(c, w) -> None:
        hits.append(c.req.method + " " + c.req.path)
        assert c.req.keep_alive is True
        assert c.req.protocol_version == "1.1"
        assert c.req.headers.get("host") == "127.0.0.1"
        responses.text(w, "ok")

    app.get("/plaintext", handler)
    proto, app, transport = _attach(app)
    try:
        proto.data_received(
            b"GET /plaintext HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"\r\n"
        )
        await _drain(app)
        assert response_status(transport.writes) == 200
        assert hits == ["GET /plaintext"]
        assert proto.llhttp_in_progress is False
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_split_headers_fall_back_to_llhttp() -> None:
    app = App()
    hits: list[str] = []

    async def handler(_c, w) -> None:
        hits.append("ok")
        responses.text(w, "ok")

    app.get("/", handler)
    proto, app, transport = _attach(app)
    try:
        proto.data_received(b"GET / HTTP/1.1\r\nHost: t\r\n")
        await asyncio.sleep(0)
        assert hits == []
        proto.data_received(b"\r\n")
        await _drain(app)
        assert response_status(transport.writes) == 200
        assert hits == ["ok"]
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_http10_keep_alive_and_close() -> None:
    app = App()

    async def handler(c, w) -> None:
        responses.text(w, "ok" if c.req.keep_alive else "close")

    app.get("/", handler)
    proto, app, transport = _attach(app)
    try:
        proto.data_received(
            b"GET / HTTP/1.0\r\nHost: t\r\nConnection: keep-alive\r\n\r\n"
        )
        await _drain(app)
        assert b"ok" in b"".join(transport.writes)
        assert not transport.is_closing()

        proto.data_received(b"GET / HTTP/1.0\r\nHost: t\r\n\r\n")
        await _drain(app)
        assert b"close" in b"".join(transport.writes)
        assert transport.is_closing()
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_duplicate_content_length_is_400() -> None:
    proto, app, transport = _attach()
    try:
        proto.data_received(
            b"POST / HTTP/1.1\r\n"
            b"Host: t\r\n"
            b"Content-Length: 1\r\n"
            b"Content-Length: 2\r\n"
            b"\r\n"
            b"ab"
        )
        await _drain(app)
        assert response_status(transport.writes) == 400
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_content_length_and_te_is_400() -> None:
    proto, app, transport = _attach()
    try:
        proto.data_received(
            b"POST / HTTP/1.1\r\n"
            b"Host: t\r\n"
            b"Content-Length: 5\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"\r\n"
            b"0\r\n\r\n"
        )
        await _drain(app)
        assert response_status(transport.writes) == 400
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_parser_is_httpprotocol() -> None:
    loop = asyncio.get_running_loop()
    proto = make_protocol(loop, App())
    assert isinstance(proto, HttpProtocol)
