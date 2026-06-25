"""HTTP protocol edge cases and bounded random input (smoke fuzz).

These tests do **not** replace dedicated fuzzers (AFL++/libFuzzer) or external audits; they
regress common parser failure modes and ensure arbitrary bytes do not escape as unhandled
exceptions from the protocol layer. See `SECURITY.md` for scope and limitations.
"""

import asyncio
import random
from typing import Any

import pytest

import stario.responses as responses
from stario import App, Relay
from stario.datastar import SSE
from stario.http.compression import CompressionConfig
from stario.http.config import RequestPolicy
from stario.http.protocol import HttpProtocol
from stario.telemetry.noop import NoOpTracer


class _RecordingTransport(asyncio.Transport):
    """Minimal `asyncio.Transport` surface used by `HttpProtocol`."""

    __slots__ = ("_closing", "_protocol", "reading_calls", "writes")

    def __init__(self, protocol: asyncio.Protocol) -> None:
        super().__init__()
        self._protocol = protocol
        self.writes: list[bytes] = []
        self._closing = False
        self.reading_calls: list[str] = []

    def write(self, data: bytes | bytearray | memoryview[Any]) -> None:
        assert not self._closing, "write after transport close"
        self.writes.append(bytes(data))

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._protocol.connection_lost(None)

    def is_closing(self) -> bool:
        return self._closing

    def pause_reading(self) -> None:
        self.reading_calls.append("pause")

    def resume_reading(self) -> None:
        self.reading_calls.append("resume")


def _make_protocol(
    *,
    max_header_bytes: int = 16_384,
    max_body_bytes: int = 16_384,
    header_timeout: float = 5.0,
    body_timeout: float = 30.0,
    keep_alive_timeout: float = 5.0,
    app: App | None = None,
) -> tuple[HttpProtocol, App, _RecordingTransport]:
    loop = asyncio.get_running_loop()
    connections: set[HttpProtocol] = set()
    if app is None:
        app = App()
    tracer = NoOpTracer()
    proto = HttpProtocol(
        loop,
        app,
        tracer,
        lambda: b"date: Thu, 01 Jan 1970 00:00:00 GMT\r\n",
        CompressionConfig(),
        connections,
        RequestPolicy(
            max_header_bytes=max_header_bytes,
            max_body_bytes=max_body_bytes,
            header_timeout=header_timeout,
            body_timeout=body_timeout,
            keep_alive_timeout=keep_alive_timeout,
        ),
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


def _response_statuses(transport: _RecordingTransport) -> list[int]:
    """Status codes of every response on the wire, in write order."""
    raw = b"".join(transport.writes)
    statuses: list[int] = []
    for part in raw.split(b"HTTP/1.1 ")[1:]:
        statuses.append(int(part.split(b" ", 1)[0]))
    return statuses


async def _drain(app: App) -> None:
    await asyncio.sleep(0)
    await app.drain_tasks()


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
async def test_invalid_incoming_header_names_return_400() -> None:
    payload = b"GET / HTTP/1.1\r\nBad Name: v\r\n\r\n"
    proto, app, transport = _make_protocol()
    try:
        proto.data_received(payload)
        await _drain(app)
        assert _response_status(transport) == 400
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_invalid_incoming_header_values_return_400() -> None:
    payload = b"GET / HTTP/1.1\r\nX-Test: ok\x00bad\r\n\r\n"
    proto, app, transport = _make_protocol()
    try:
        proto.data_received(payload)
        await _drain(app)
        assert _response_status(transport) == 400
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_header_total_over_limit_returns_431() -> None:
    limit = 512
    app = App()
    hits = 0

    async def handler(c, w) -> None:
        nonlocal hits
        hits += 1
        responses.text(w, "should not run")

    app.get("/", handler)
    proto, app, transport = _make_protocol(
        app=app,
        max_header_bytes=limit,
    )
    try:
        # Large header name+value pushes cumulative head bytes past `limit`.
        pad = b"x" * (limit + 32)
        proto.data_received(b"GET / HTTP/1.1\r\nHost: t\r\nX-Pad: " + pad + b"\r\n\r\n")
        await _drain(app)
        assert _response_status(transport) == 431
        assert hits == 0
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_body_over_limit_returns_413() -> None:
    app = App()

    async def read_body(c, w) -> None:
        await c.req.body()

        responses.text(w, "ok")

    app.post("/", read_body)

    proto, app, transport = _make_protocol(
        app=app,
        max_header_bytes=8192,
        max_body_bytes=20,
    )
    try:
        proto.data_received(
            b"POST / HTTP/1.1\r\nHost: t\r\nContent-Length: 100\r\n\r\n" + (b"y" * 100)
        )
        await _drain(app)
        assert _response_status(transport) == 413
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_declared_body_over_limit_fails_before_handler_runs() -> None:
    app = App()
    hits = 0

    async def ignore_body(c, w) -> None:
        nonlocal hits
        hits += 1
        responses.text(w, "ok")

    app.post("/", ignore_body)

    proto, app, transport = _make_protocol(app=app, max_body_bytes=20)
    try:
        proto.data_received(
            b"POST / HTTP/1.1\r\nHost: t\r\nContent-Length: 100\r\n\r\n"
        )
        await _drain(app)
        assert _response_status(transport) == 413
        assert hits == 0
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_random_inputs_do_not_raise() -> None:
    seed, rounds = 42, 15
    rng = random.Random(seed)

    for _ in range(rounds):
        proto, app, transport = _make_protocol()
        try:
            n = rng.randint(0, 512)
            payload = rng.randbytes(n)
            proto.data_received(payload)
            await _drain(app)

            # Invariant: random bytes either produce no response yet (parser
            # still hungry), a well-formed error/routing response, or a
            # closed connection. Never a write after close (transport
            # asserts) and never an unknown status.
            status = _response_status(transport)
            assert status in (None, 400, 404, 413, 431, 414, 505)
        finally:
            if not transport.is_closing():
                transport.close()
            await _drain(app)


@pytest.mark.asyncio
async def test_percent_encoded_path_reaches_handler() -> None:
    app = App()
    seen: list[str] = []

    async def handler(c, w) -> None:
        seen.append(c.req.path)
        responses.text(w, "ok")

    app.get("/hello world", handler)

    proto, app, transport = _make_protocol(app=app)
    try:
        proto.data_received(b"GET /hello%20world HTTP/1.1\r\nHost: t\r\n\r\n")
        await _drain(app)
        assert _response_status(transport) == 200
        assert seen == ["/hello world"]
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_percent_encoded_slash_does_not_change_route_structure() -> None:
    app = App()
    seen: list[str] = []

    async def wildcard(c, w) -> None:
        seen.append(c.route.params["name"])
        responses.text(w, "wildcard")

    async def nested(c, w) -> None:
        seen.append("nested")
        responses.text(w, "nested")

    app.get("/files/{name}", wildcard)
    app.get("/files/a/b", nested)

    proto, app, transport = _make_protocol(app=app)
    try:
        proto.data_received(b"GET /files/a%2Fb HTTP/1.1\r\nHost: t\r\n\r\n")
        await _drain(app)
        assert _response_status(transport) == 200
        assert seen == ["a%2Fb"]
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_partial_header_times_out() -> None:
    proto, app, transport = _make_protocol(header_timeout=0.01)
    try:
        proto.data_received(b"GET / HTTP/1.1\r\n")
        await asyncio.sleep(0.02)
        assert transport.is_closing()
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_started_stream_failure_closes_without_terminal_chunk() -> None:
    app = App()

    async def handler(c, w) -> None:
        w.write(b"partial")
        raise RuntimeError("stream failed")

    app.get("/", handler)
    proto, app, transport = _make_protocol(app=app)
    try:
        proto.data_received(b"GET / HTTP/1.1\r\nHost: t\r\n\r\n")
        await _drain(app)
        raw = b"".join(transport.writes)
        assert _response_status(transport) == 200
        assert b"7\r\npartial\r\n" in raw
        assert not raw.endswith(b"0\r\n\r\n")
        assert transport.is_closing()
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_pipelined_requests_are_served_in_order() -> None:
    """Two full requests in one TCP segment: handlers run FIFO, never overlapped."""
    app = App()
    order: list[str] = []

    async def slow(c, w) -> None:
        order.append("slow.start")
        await asyncio.sleep(0.01)
        order.append("slow.end")
        responses.text(w, "slow")

    async def fast(c, w) -> None:
        order.append("fast")
        responses.text(w, "fast")

    app.get("/slow", slow)
    app.get("/fast", fast)

    proto, app, transport = _make_protocol(app=app)
    try:
        proto.data_received(
            b"GET /slow HTTP/1.1\r\nHost: t\r\n\r\n"
            b"GET /fast HTTP/1.1\r\nHost: t\r\n\r\n"
        )
        await _drain(app)
        await app.drain_tasks()
        await _drain(app)

        # Second handler must not start until the first response finished.
        assert order == ["slow.start", "slow.end", "fast"]
        assert _response_statuses(transport) == [200, 200]

        raw = b"".join(transport.writes)
        assert raw.index(b"slow") < raw.index(b"fast")
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_chunked_request_body_reaches_handler() -> None:
    app = App()
    bodies: list[bytes] = []

    async def echo(c, w) -> None:
        bodies.append(await c.req.body())
        responses.text(w, "ok")

    app.post("/", echo)

    proto, app, transport = _make_protocol(app=app)
    try:
        proto.data_received(
            b"POST / HTTP/1.1\r\n"
            b"Host: t\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"\r\n"
            b"5\r\nhello\r\n"
            b"6\r\n world\r\n"
            b"0\r\n\r\n"
        )
        await _drain(app)
        assert _response_status(transport) == 200
        assert bodies == [b"hello world"]
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_expect_100_continue_sends_interim_response() -> None:
    app = App()

    async def echo(c, w) -> None:
        body = await c.req.body()
        responses.text(w, body.decode())

    app.post("/", echo)

    proto, app, transport = _make_protocol(app=app)
    try:
        proto.data_received(
            b"POST / HTTP/1.1\r\n"
            b"Host: t\r\n"
            b"Content-Length: 5\r\n"
            b"Expect: 100-continue\r\n"
            b"\r\n"
        )
        await asyncio.sleep(0)  # let the handler start reading the body

        raw = b"".join(transport.writes)
        assert raw.startswith(b"HTTP/1.1 100 Continue\r\n\r\n")

        proto.data_received(b"hello")
        await _drain(app)

        assert _response_statuses(transport) == [100, 200]
        assert b"".join(transport.writes).endswith(b"hello")
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_connection_close_header_closes_socket_after_response() -> None:
    app = App()

    async def handler(c, w) -> None:
        responses.text(w, "bye")

    app.get("/", handler)

    proto, app, transport = _make_protocol(app=app)
    try:
        proto.data_received(b"GET / HTTP/1.1\r\nHost: t\r\nConnection: close\r\n\r\n")
        await _drain(app)

        assert _response_status(transport) == 200
        assert transport.is_closing()
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        b"GET /caf% HTTP/1.1\r\nHost: t\r\n\r\n",
        b"GET /%C3 HTTP/1.1\r\nHost: t\r\n\r\n",
        b"GET /caf\xc3 HTTP/1.1\r\nHost: t\r\n\r\n",
    ],
    ids=["truncated_percent", "invalid_utf8_percent", "invalid_utf8_path"],
)
async def test_invalid_path_encoding_returns_400(payload: bytes) -> None:
    proto, app, transport = _make_protocol()
    try:
        proto.data_received(payload)
        await _drain(app)
        assert _response_status(transport) == 400
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        b"GET / HTTP/1.1\r\nHost: t\r\nContent-Length: 0\r\nContent-Length: 0\r\n\r\n",
        b"POST / HTTP/1.1\r\nHost: t\r\nTransfer-Encoding: gzip\r\n\r\n",
        b"POST / HTTP/1.1\r\n"
        b"Host: t\r\n"
        b"Content-Length: 5\r\n"
        b"Transfer-Encoding: chunked\r\n"
        b"\r\n",
    ],
    ids=[
        "duplicate_content_length",
        "unsupported_transfer_encoding",
        "content_length_with_chunked",
    ],
)
async def test_invalid_content_length_or_transfer_encoding_returns_400(
    payload: bytes,
) -> None:
    proto, app, transport = _make_protocol()
    try:
        proto.data_received(payload)
        await _drain(app)
        assert _response_status(transport) == 400
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_close_with_error_signals_disconnect_without_cancelling_active_handler() -> (
    None
):
    app = App()
    first_started = asyncio.Event()
    hang = asyncio.Event()
    first_finished = asyncio.Event()

    async def first(c, _w) -> None:
        first_started.set()
        async with c.alive():
            await hang.wait()
        first_finished.set()

    async def second(_c, _w) -> None:
        responses.text(_w, "should not run")

    app.get("/first", first)
    app.post("/second", second)

    proto, app, transport = _make_protocol(app=app)
    try:
        proto.data_received(b"GET /first HTTP/1.1\r\nHost: t\r\n\r\n")
        await first_started.wait()

        proto.data_received(
            b"POST /second HTTP/1.1\r\nHost: t\r\nContent-Length: 5\r\n\r\n"
        )
        await asyncio.sleep(0)

        proto.data_received(b"not http\r\n")
        await asyncio.sleep(0)
        await _drain(app)

        assert first_finished.is_set()
        assert _response_status(transport) == 400
    finally:
        hang.set()
        if not transport.is_closing():
            transport.close()
        await _drain(app)


@pytest.mark.asyncio
async def test_connection_lost_signals_disconnect_without_cancelling_handler() -> None:
    app = App()
    started = asyncio.Event()
    hang = asyncio.Event()
    finished = asyncio.Event()

    async def handler(c, _w) -> None:
        started.set()
        async with c.alive():
            await hang.wait()
        finished.set()

    app.get("/", handler)

    proto, app, transport = _make_protocol(app=app)
    try:
        proto.data_received(b"GET / HTTP/1.1\r\nHost: t\r\n\r\n")
        await started.wait()
        transport.close()
        await asyncio.sleep(0)
        await _drain(app)
        assert finished.is_set()
    finally:
        hang.set()
        await _drain(app)


@pytest.mark.asyncio
async def test_connection_lost_lets_sse_handler_run_post_alive_cleanup() -> None:
    """Disconnect must not hard-cancel the handler before post-loop SSE teardown."""
    app = App()
    relay = Relay[str]()
    cleanup_events: list[str] = []

    async def subscribe(c, w) -> None:
        async with relay.subscribe("*") as live:
            SSE(w).open()
            c.span.event("connected", {})
            async for _subject, _ in c.alive(live):
                pass
        cleanup_events.append("disconnected")
        c.span.event("disconnected", {})

    app.get("/subscribe", subscribe)

    proto, app, transport = _make_protocol(app=app)
    try:
        proto.data_received(b"GET /subscribe HTTP/1.1\r\nHost: t\r\n\r\n")
        await asyncio.sleep(0)
        transport.close()
        await _drain(app)
        assert cleanup_events == ["disconnected"]
    finally:
        await _drain(app)


@pytest.mark.asyncio
async def test_keep_alive_serves_second_request_on_same_connection() -> None:
    app = App()
    hits: list[str] = []

    async def a(c, w) -> None:
        hits.append("a")
        responses.text(w, "a")

    async def b(c, w) -> None:
        hits.append("b")
        responses.text(w, "b")

    app.get("/a", a)
    app.get("/b", b)

    proto, app, transport = _make_protocol(app=app)
    try:
        proto.data_received(b"GET /a HTTP/1.1\r\nHost: t\r\n\r\n")
        await _drain(app)
        assert _response_status(transport) == 200
        assert hits == ["a"]

        proto.data_received(b"GET /b HTTP/1.1\r\nHost: t\r\n\r\n")
        await _drain(app)
        assert _response_status(transport) == 200
        assert hits == ["a", "b"]
        assert not transport.is_closing()
    finally:
        if not transport.is_closing():
            transport.close()
        await _drain(app)
