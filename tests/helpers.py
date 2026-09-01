"""Shared test helpers for the stario test suite.

Plain importable helpers (not fixtures) so test modules can compose them
freely: `from tests.helpers import DummyWriter, make_context, ...`.
"""

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any, cast
from urllib.parse import urlencode

from stario.http.app import App
from stario.http.context import Context
from stario.http.headers import Headers
from stario.http.writer import Writer
from stario.telemetry.noop import NoOpTracer
from stario.testing.harness import TestContext, TestRequest, TestWriter
from stario.testing.tracer import TestTracer

type AppSetup = Callable[[App], None]


def app_for_loop(_loop: asyncio.AbstractEventLoop) -> App:
    return App()


class DummyWriter:
    """Minimal Writer stand-in for dispatch-level router/app tests.

    Tracks the response surface the router/app touches: status, body,
    headers, started/ended/completed flags.
    """

    def __init__(self) -> None:
        self.status: int | None = None
        self.body: str | None = None
        self.headers = Headers()
        self.started = False
        self.ended = False
        self._status_code: int | None = None
        self._completed = False

    @property
    def status_code(self) -> int | None:
        return self._status_code

    @property
    def completed(self) -> bool:
        return self._completed

    def respond(self, body: bytes, content_type: bytes, status: int = 200) -> None:
        self.body = body.decode("utf-8")
        self.status = status
        self.started = True
        self._status_code = status
        self.headers.set("content-type", content_type.decode("latin-1"))
        self.ended = True
        self._completed = True

    def write_headers(self, status: int):
        self.status = status
        self.started = True
        self._status_code = status
        return self

    def write(self, data: bytes):
        if data:
            self.body = data.decode("utf-8")
        return self

    def end(self, data: bytes | None = None) -> None:
        if data is not None:
            self.body = data.decode("utf-8")
        self.ended = True
        self._completed = True
        return None

    def abort(self) -> None:
        self.ended = False
        self._completed = True


def make_request(
    *,
    method: str = "GET",
    path: str = "/",
    host: str = "",
    headers: dict[str, str] | None = None,
    body: bytes = b"",
    query: dict[str, object] | None = None,
    query_bytes: bytes | None = None,
) -> TestRequest:
    hdrs = Headers()
    if headers:
        for name, value in headers.items():
            hdrs.set(name, value)
    if host:
        hdrs.set("host", host)

    if query_bytes is None:
        query_bytes = urlencode(query or {}, doseq=True).encode("ascii")

    return TestRequest(
        method=method,
        path=path,
        query_bytes=query_bytes,
        headers=hdrs,
        body=body,
    )


def make_context(
    path: str = "/",
    method: str = "GET",
    host: str = "",
    *,
    app: App | None = None,
    query: dict[str, object] | None = None,
    loop: asyncio.AbstractEventLoop,
    disconnect: asyncio.Future[Any] | None = None,
) -> TestContext:
    if disconnect is None:
        disconnect = loop.create_future()
    if app is None:
        app = App()
    tracer = NoOpTracer()
    return TestContext(
        app=app,
        req=make_request(method=method, path=path, host=host, query=query),
        span=tracer.create("request"),
        _disconnect=disconnect,
        state={},
    )


async def invoke_app(
    app: App,
    path: str = "/",
    method: str = "GET",
    host: str = "",
    *,
    query: dict[str, object] | None = None,
    writer: DummyWriter | None = None,
) -> tuple[TestContext, DummyWriter]:
    loop = asyncio.get_running_loop()
    ctx = make_context(path, method, host, app=app, query=query, loop=loop)
    w = writer or DummyWriter()
    await app(ctx, cast(Writer, w))
    return ctx, w


def run_with_app(
    setup: AppSetup | App,
    path: str = "/",
    method: str = "GET",
    host: str = "",
    *,
    query: dict[str, object] | None = None,
) -> tuple[TestContext, DummyWriter]:
    async def _run() -> tuple[TestContext, DummyWriter]:
        loop = asyncio.get_running_loop()
        if isinstance(setup, App):
            app = setup
        else:
            app = app_for_loop(loop)
            setup(app)
        return await invoke_app(app, path, method, host, query=query)

    return asyncio.run(_run())


async def invoke_handler(
    handler: Any,
    path: str = "/",
    method: str = "GET",
    host: str = "",
    *,
    app: App | None = None,
    query: dict[str, object] | None = None,
    writer: DummyWriter | None = None,
) -> tuple[TestContext, DummyWriter]:
    loop = asyncio.get_running_loop()
    ctx = make_context(path, method, host, app=app, query=query, loop=loop)
    w = writer or DummyWriter()
    await handler(ctx, cast(Writer, w))
    return ctx, w


def run_handler(
    handler: Any,
    path: str = "/",
    method: str = "GET",
    host: str = "",
    *,
    app: App | None = None,
    query: dict[str, object] | None = None,
    writer: DummyWriter | None = None,
) -> tuple[TestContext, DummyWriter]:
    return asyncio.run(
        invoke_handler(
            handler,
            path,
            method,
            host,
            app=app,
            query=query,
            writer=writer,
        )
    )


def run_async(awaitable: Awaitable[None]) -> None:
    asyncio.run(cast(Coroutine[Any, Any, None], awaitable))


def make_writer() -> TestWriter:
    """A TestClient-style writer that collects status, headers, and body."""
    return TestWriter()


class _ClosedLoop:
    def close(self) -> None:
        return None


def make_writer_raw() -> tuple[TestWriter, bytearray, _ClosedLoop]:
    """TestWriter plus its body buffer (not HTTP/1.1 wire bytes)."""
    writer = TestWriter()
    return writer, writer.sink.buf, _ClosedLoop()


def split_response(raw: bytes) -> tuple[bytes, bytes]:
    """Split raw HTTP/1.1 wire bytes into (head, body)."""
    head, _, body = raw.partition(b"\r\n\r\n")
    return head, body


def assert_status_span(
    tracer: TestTracer,
    status: int,
    *,
    method: str | None = None,
    path: str | None = None,
) -> None:
    """Assert a finished request span recorded `status` and was not failed."""
    matches = [
        span
        for span in tracer.finished_spans()
        if span.attributes.get("response.status_code") == status
    ]
    assert matches, f"no finished span with status {status}: {tracer.finished_spans()}"
    span = matches[0]
    assert span.ok
    if method is not None:
        assert span.attributes.get("request.method") == method
    if path is not None:
        assert span.attributes.get("request.path") == path
    assert not tracer.has_open_spans()
